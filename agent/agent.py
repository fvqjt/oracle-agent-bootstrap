#!/usr/bin/env python3
"""Oracle Runner Agent — pure-stdlib client for Oracle Agent Protocol v1.

Runs on the Runner with NO third-party dependencies. All communication is
outbound to the Oracle server:
    register -> heartbeat/pull loop -> execute task -> report result

Config:     config.json (oracle_url, agent_name, capabilities, ...)
State:      state.json (agent_id + token, created on first register, chmod 600)
Run:        python3 agent.py
"""
import json
import os
import re
import secrets
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from handlers import HANDLERS, HandlerError, HandlerTimeout
from ws_client import WSClient
from handlers.exec_pty import handle_exec_pty, handle_pty_input
import mcp_proxy

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

CONFIG = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
ORACLE_URL = (CONFIG.get("oracle_url") or "").rstrip("/")
PULL_TIMEOUT = int(CONFIG.get("pull_timeout_sec", 30))
HEARTBEAT_INTERVAL = int(CONFIG.get("heartbeat_interval_sec", 60))
USER_AGENT = CONFIG.get("user_agent") or ("oracle-agent/" + str(CONFIG.get("version", "0.1.0")))

AGENT_ID = None
TOKEN = None
STOP = False
_pty_sessions = {}  # session_id -> state dict
_ws_client = None   # global WSClient instance


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{ts}] {msg}", flush=True)


class AgentHTTPError(Exception):
    def __init__(self, code: int, detail):
        super().__init__(f"HTTP {code}: {json.dumps(detail, ensure_ascii=False)}")
        self.code = code
        self.detail = detail


def _request(path: str, body: dict, token: str | None = None, timeout: float = 30):
    url = ORACLE_URL + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except Exception:
            detail = {"message": str(e)}
        raise AgentHTTPError(e.code, detail) from e
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as e:
        # 网络/超时错误必须转成可重试错误，否则会炸掉主循环被 systemd 反复拉起
        raise AgentHTTPError(0, {"message": f"network error: {e}"}) from e


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_PATH)


_AZURE_META = None


def azure_metadata() -> dict:
    """Azure Instance Metadata (IMDS): 全局唯一 VM 身份。非 Azure 环境返回空 dict。"""
    global _AZURE_META
    if _AZURE_META is None:
        try:
            req = urllib.request.Request(
                "http://169.254.169.254/metadata/instance?api-version=2021-02-01",
                headers={"Metadata": "true"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                _AZURE_META = json.loads(resp.read().decode("utf-8"))
        except Exception:
            _AZURE_META = {}
    return _AZURE_META


_PUBLIC_IP = None


def public_ip() -> str:
    """出口公网 IP（GH Runner 无网卡公网 IP，必须经出站探测）。"""
    global _PUBLIC_IP
    if _PUBLIC_IP is None:
        try:
            req = urllib.request.Request("https://ifconfig.io/ip", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=5) as resp:
                _PUBLIC_IP = resp.read().decode("utf-8", "replace").strip()
        except Exception:
            _PUBLIC_IP = ""
    return _PUBLIC_IP


def default_agent_id() -> str:
    # GitHub 会复用 VM 主机名；纯 hostname 做 agent_id 会与历史记录碰撞(403)。
    # 优先用 Azure VM instance ID（全局唯一、稳定）做唯一性来源，取不到再回退随机后缀。
    host = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname() or "unknown")
    vm = str(azure_metadata().get("compute", {}).get("vmId") or "")
    uniq = vm[:8] if vm else secrets.token_hex(3)
    return f"runner-{host}-{uniq}"


def register() -> tuple[str, str]:
    state = load_state()
    az = azure_metadata().get("compute") or {}
    imds = azure_metadata() or {}
    body = {
        "agent_id": state.get("agent_id") or CONFIG.get("agent_id") or default_agent_id(),
        "token": state.get("token"),
        "name": CONFIG.get("agent_name") or socket.gethostname(),
        "version": CONFIG.get("version"),
        "capabilities": CONFIG.get("capabilities", []),
        "meta": {
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "run_id": CONFIG.get("run_id"),
            "public_ip": public_ip(),
            "azure_vm_id": az.get("vmId") or "",
            "azure_vm_name": az.get("name") or "",
            "imds": imds,
        },
    }
    try:
        resp = _request("/api/v1/agent/register", body, timeout=15)
    except AgentHTTPError as e:
        if e.code == 403 and "agent_id already exists" in str(e.detail):
            # id 碰撞（主机名复用等）：换一个新 id 重试
            fresh = f"{default_agent_id()}"
            log(f"agent_id collision; re-registering as {fresh} ({e})")
            body["agent_id"] = fresh
            resp = _request("/api/v1/agent/register", body, timeout=15)
        else:
            raise
    agent_id = resp["agent_id"]
    token = resp.get("token") or state.get("token")
    if agent_id and token:
        save_state({"agent_id": agent_id, "token": token})
    return agent_id, token


def heartbeat() -> None:
    _request(
        "/api/v1/agent/heartbeat",
        {
            "agent_id": AGENT_ID,
            "status": "online",
            "version": CONFIG.get("version"),
            "capabilities": CONFIG.get("capabilities", []),
        },
        token=TOKEN,
        timeout=15,
    )


def pull() -> dict:
    return _request(
        "/api/v1/agent/pull",
        {"agent_id": AGENT_ID, "timeout_sec": PULL_TIMEOUT},
        token=TOKEN,
        timeout=PULL_TIMEOUT + 10,
    )


def execute(task: dict) -> tuple[str, dict | None, str | None]:
    type_ = task.get("type")
    params = task.get("params") or {}
    handler = HANDLERS.get(type_)
    if not handler:
        return "rejected", None, f"unknown task type: {type_}"
    try:
        output = handler(params)
        return "success", output, None
    except HandlerTimeout as e:
        return "timeout", e.output, e.message
    except HandlerError as e:
        return e.status, e.output, e.message
    except Exception as e:  # defensive: never let one bad task kill the loop
        return "failed", None, f"{type(e).__name__}: {e}"


def report_result(task_id: str, status: str, output, error, started_at: str, finished_at: str) -> None:
    _request(
        "/api/v1/agent/result",
        {
            "agent_id": AGENT_ID,
            "task_id": task_id,
            "status": status,
            "output": output,
            "error": error,
            "started_at": started_at,
            "finished_at": finished_at,
        },
        token=TOKEN,
        timeout=15,
    )


def report_event(event: str, data=None) -> None:
    try:
        _request(
            "/api/v1/agent/report",
            {
                "agent_id": AGENT_ID,
                "event": event,
                "data": data,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            token=TOKEN,
            timeout=15,
        )
    except AgentHTTPError as e:
        log(f"report {event} failed: {e}")


def _signal_handler(signum, _frame) -> None:
    global STOP
    STOP = True
    log(f"received signal {signum}, stopping gracefully")



def _on_ws_message(msg: dict):
    """Handle messages from server via WebSocket."""
    msg_type = msg.get("type")
    session_id = msg.get("session_id", "")

    if msg_type == "exec_start":
        command = msg.get("command", ["bash"])
        pty_flag = msg.get("pty", True)
        if pty_flag:
            state = handle_exec_pty(msg, _ws_client)
            if state:
                _pty_sessions[session_id] = state
        else:
            # Non-PTY exec via WS: run and send result back
            import subprocess
            timeout = msg.get("timeout_sec", 60)
            try:
                proc = subprocess.run(
                    [str(c) for c in command],
                    capture_output=True, text=True, timeout=timeout,
                )
                _ws_client.send_json({
                    "type": "output", "session_id": session_id,
                    "stream": "stdout", "data": proc.stdout,
                })
                if proc.stderr:
                    _ws_client.send_json({
                        "type": "output", "session_id": session_id,
                        "stream": "stderr", "data": proc.stderr,
                    })
                _ws_client.send_json({"type": "exit", "session_id": session_id, "code": proc.returncode})
            except subprocess.TimeoutExpired:
                _ws_client.send_json({"type": "exit", "session_id": session_id, "code": -1})
            except Exception as e:
                _ws_client.send_json({"type": "error", "session_id": session_id, "message": str(e)})

    elif msg_type in ("stdin", "signal", "resize"):
        state = _pty_sessions.get(session_id)
        if state:
            handle_pty_input(state, msg)

    elif msg_type == "exec_end":
        state = _pty_sessions.pop(session_id, None)
        if state:
            handle_pty_input(state, {"type": "exec_end"})

    elif msg_type == "mcp_call":
        mcp_proxy.handle_mcp_call(msg, _ws_client)


def _start_ws_client():
    """Start WebSocket client thread, returns WSClient instance."""
    global _ws_client
    if not TOKEN or not AGENT_ID:
        return None
    ws_url = f"{ORACLE_URL}/api/v1/agent/ws?token={TOKEN}&agent_id={AGENT_ID}"
    client = WSClient(ws_url, on_message=_on_ws_message)
    client.start()
    _ws_client = client
    return client


def main() -> int:
    global AGENT_ID, TOKEN, STOP
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    if not ORACLE_URL:
        log("error: config.json missing or oracle_url empty")
        return 1

    # Register (or re-register) once at startup.
    for attempt in range(1, 6):
        try:
            AGENT_ID, TOKEN = register()
            break
        except AgentHTTPError as e:
            log(f"register failed ({attempt}/5): {e}")
            time.sleep(5)
    else:
        log("register failed after 5 attempts, giving up")
        return 1
    log(f"registered as {AGENT_ID}")

    last_heartbeat = 0.0
    ws = None  # WS client not yet wired up; fall back to pull loop
    while not STOP:
        now = time.time()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            try:
                heartbeat()
                last_heartbeat = now
            except AgentHTTPError as e:
                log(f"heartbeat failed: {e}")

        # If WS is connected, skip pull (WS handles task dispatch)
        if ws and ws.is_connected():
            time.sleep(1)
            continue

        # WS disconnected: fall back to pull loop
        try:
            resp = pull()
        except AgentHTTPError as e:
            log(f"pull failed: {e}")
            time.sleep(5)
            continue

        task = resp.get("task")
        if not task:
            continue

        task_id = task.get("task_id", "?")
        log(f"task {task_id[:12]} type={task.get('type')} started")
        started_at = datetime.now(timezone.utc).isoformat()
        status, output, error = execute(task)
        finished_at = datetime.now(timezone.utc).isoformat()
        try:
            report_result(task_id, status, output, error, started_at, finished_at)
            log(f"task {task_id[:12]} -> {status}")
        except AgentHTTPError as e:
            log(f"report failed: {e}")

    log("agent stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())