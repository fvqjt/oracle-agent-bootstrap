#!/usr/bin/env python3
"""Oracle Runner Agent — pure-stdlib client for Oracle Agent Protocol v1.

Runs on the Runner with NO third-party dependencies. All communication is
outbound to the Oracle server:
    register -> heartbeat/pull loop -> execute task -> report result

Concurrency model (v0.2):
    pull-loop 主线程只做 pull + heartbeat + 提交；execute 跑在
    ThreadPoolExecutor (max_workers = max_concurrent, 默认 32, 0=无界)
    里，支持同时跑 N 条任务，sleep / 长任务不会阻塞后续命令。

Config:     config.json (oracle_url, agent_name, capabilities, max_concurrent, ...)
State:      state.json (agent_id + token, created on first register, chmod 600)
Run:        python3 agent.py
"""
import concurrent.futures
import json
import os
import re
import secrets
import signal
import socket
import sys
import threading
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

# 并发上限：max_concurrent=0 表示无界（每个任务一个线程）；>0 则作为线程池上限。
# 默认 32，对 runner 足够；设 0 则彻底无界。
MAX_CONCURRENT = int(CONFIG.get("max_concurrent", 32))

AGENT_ID = None
TOKEN = None
STOP = False
_pty_sessions = {}  # session_id -> state dict
_ws_client = None   # global WSClient instance

# 共享 ThreadPoolExecutor: 0 => 无界, 用 threading.Thread per task
_executor: "concurrent.futures.ThreadPoolExecutor | None" = None
_executor_lock = threading.Lock()
_active_tasks: set = set()  # 正在跑的任务 task_id（用于日志统计）
_active_lock = threading.Lock()


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
    except Exception as e:
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


# ---------------------------------------------------------------------------
# 并发执行：pull-loop 主线程不阻塞，execute 跑在 worker 里
# ---------------------------------------------------------------------------

def _execute_and_report(task: dict) -> None:
    """Worker 线程: 跑一个任务并回报结果（异常兜底，不影响其他任务）。"""
    task_id = task.get("task_id", "?")
    type_ = task.get("type")
    with _active_lock:
        _active_tasks.add(task_id)
        log(f"task {task_id[:12]} type={type_} start (in-flight={len(_active_tasks)})")
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        status, output, error = execute(task)
    except Exception as e:
        status, output, error = "failed", None, f"{type(e).__name__}: {e}"
    finished_at = datetime.now(timezone.utc).isoformat()
    try:
        report_result(task_id, status, output, error, started_at, finished_at)
        log(f"task {task_id[:12]} -> {status}")
    except AgentHTTPError as e:
        log(f"task {task_id[:12]} report failed: {e}")
    finally:
        with _active_lock:
            _active_tasks.discard(task_id)


def _submit(task: dict) -> None:
    """把任务提交到线程池 / 新线程（无界模式）。"""
    global _executor
    if MAX_CONCURRENT <= 0:
        # 无界：每个任务开一个线程
        t = threading.Thread(target=_execute_and_report, args=(task,), daemon=True)
        t.start()
        return
    with _executor_lock:
        if _executor is None:
            _executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=MAX_CONCURRENT,
                thread_name_prefix="oracle-task",
            )
        _executor.submit(_execute_and_report, task)


def main() -> int:
    global AGENT_ID, TOKEN, STOP
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    if not ORACLE_URL:
        log("error: config.json missing or oracle_url empty")
        return 1

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
    ws = None
    while not STOP:
        now = time.time()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            try:
                heartbeat()
                last_heartbeat = now
            except AgentHTTPError as e:
                log(f"heartbeat failed: {e}")

        if ws and ws.is_connected():
            time.sleep(1)
            continue

        try:
            resp = pull()
        except AgentHTTPError as e:
            log(f"pull failed: {e}")
            time.sleep(5)
            continue

        task = resp.get("task")
        if not task:
            continue

        _submit(task)  # 非阻塞提交；execute 在 worker 里跑，主循环继续 pull

    # 优雅退出：等所有 in-flight 任务完成（最多 10s）
    with _active_lock:
        remaining = len(_active_tasks)
    if remaining:
        log(f"waiting for {remaining} in-flight task(s) to finish...")
        deadline = time.time() + 10
        while time.time() < deadline:
            with _active_lock:
                if not _active_tasks:
                    break
            time.sleep(0.5)
    if _executor is not None:
        _executor.shutdown(wait=False)

    log("agent stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())