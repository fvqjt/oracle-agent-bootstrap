"""
MCP Shell Server v2
- MCP Python SDK v2 / MCPServer
- run_command
- Interactive PTY sessions
- Background processes
- Direct file read/write
- SSE compatibility for legacy clients
- Streamable HTTP for modern MCP clients

Listen:
    127.0.0.1:6942

Endpoints:
    /sse
    /messages
    /mcp
    /health
"""

from mcp.server import MCPServer

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import pty
import shlex
import signal
import struct
import termios
import time
import uuid
import uvicorn
from starlette.applications import Starlette
from starlette.responses import Response

# ============================================================
# Configuration
# ============================================================

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 6942

DEFAULT_TIMEOUT = 30
OUTPUT_MAX_BYTES = 1024 * 1024
SESSION_IDLE_TIMEOUT = 1800

LOG_FILE = "/tmp/mcp_shell.log"

# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("mcp_shell_v2")

# ============================================================
# MCP Server
# ============================================================

mcp = MCPServer("shell-extended")

# ============================================================
# Command result
# ============================================================


class CommandResult:
    def __init__(self, stdout, stderr, exit_code, timed_out):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.timed_out = timed_out

    def to_dict(self):
        return {
            "stdout": self.stdout[-OUTPUT_MAX_BYTES:],
            "stderr": self.stderr[-OUTPUT_MAX_BYTES:],
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
        }


# ============================================================
# Simple command execution
# ============================================================


@mcp.tool()
async def run_command(command: str, timeout: int = DEFAULT_TIMEOUT):
    """
    Execute a shell command and return stdout, stderr and exit code.
    """

    log.info("[run_command] executing: %s", command[:200])

    try:
        t = int(timeout) if timeout is not None else DEFAULT_TIMEOUT
    except (ValueError, TypeError):
        t = DEFAULT_TIMEOUT

    actual_timeout = t if t > 0 else DEFAULT_TIMEOUT

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=lambda: (
                os.setpgrp(),
                signal.signal(signal.SIGTERM, signal.SIG_DFL),
            ),
        )

        timed_out = False
        stdout = b""
        stderr = b""

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=actual_timeout,
            )

        except asyncio.TimeoutError:
            timed_out = True

            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

            await proc.wait()

        stdout_decoded = stdout.decode(errors="replace") if stdout else ""
        stderr_decoded = stderr.decode(errors="replace") if stderr else ""

        result = CommandResult(
            stdout=stdout_decoded,
            stderr=stderr_decoded,
            exit_code=(
                proc.returncode
                if proc.returncode is not None
                else -1
            ),
            timed_out=timed_out,
        )

        log.info(
            "[run_command] exit=%s timed_out=%s",
            result.exit_code,
            result.timed_out,
        )

        return json.dumps(result.to_dict())

    except Exception as e:
        log.exception("[run_command] error")

        return json.dumps(
            {
                "stdout": "",
                "stderr": f"Error: {e}",
                "exit_code": -1,
                "timed_out": False,
            }
        )


# ============================================================
# PTY Session
# ============================================================


class PTYSession:
    def __init__(self, session_id, command):
        self.session_id = session_id
        self.command = command

        self.master_fd = None
        self.child_pid = None

        self.output_buffer = ""

        self.last_active = time.time()
        self.closed = False

        self._reader_task = None

    def is_alive(self):
        if self.closed or self.child_pid is None:
            return False

        try:
            pid, _ = os.waitpid(
                self.child_pid,
                os.WNOHANG,
            )

            if pid == self.child_pid:
                self.closed = True
                return False

            return True

        except ChildProcessError:
            self.closed = True
            return False

    def close(self):

        if self.closed:
            return

        self.closed = True

        if self._reader_task:
            self._reader_task.cancel()

        if self.child_pid:

            try:
                pgid = os.getpgid(self.child_pid)
                os.killpg(
                    pgid,
                    signal.SIGKILL,
                )

            except Exception:
                try:
                    os.kill(
                        self.child_pid,
                        signal.SIGKILL,
                    )
                except Exception:
                    pass

            try:
                os.waitpid(
                    self.child_pid,
                    os.WNOHANG,
                )
            except ChildProcessError:
                pass

        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except Exception:
                pass

        self.master_fd = None
        self.child_pid = None


sessions = {}

_session_lock = asyncio.Lock()

# ============================================================
# PTY helpers
# ============================================================


def _set_nonblocking(fd):

    flags = fcntl.fcntl(
        fd,
        fcntl.F_GETFL,
    )

    fcntl.fcntl(
        fd,
        fcntl.F_SETFL,
        flags | os.O_NONBLOCK,
    )


# ============================================================
# PTY reader
# ============================================================


async def _reader_loop(session):

    loop = asyncio.get_running_loop()

    fd = session.master_fd

    def _on_readable():

        try:
            data = os.read(
                fd,
                65536,
            )

        except (BlockingIOError, OSError):
            return

        if not data:

            session.closed = True

            asyncio.create_task(
                _cleanup_session_async(session)
            )

            return

        decoded = data.decode(
            errors="replace"
        )

        session.output_buffer += decoded

        if len(session.output_buffer) > OUTPUT_MAX_BYTES * 2:

            session.output_buffer = (
                session.output_buffer[-OUTPUT_MAX_BYTES:]
            )

        session.last_active = time.time()

    try:

        loop.add_reader(
            fd,
            _on_readable,
        )

        while (
            not session.closed
            and session.master_fd is not None
        ):

            await asyncio.sleep(0.5)

    except asyncio.CancelledError:
        pass

    except Exception as e:

        log.exception(
            "[reader_loop] %s",
            session.session_id,
        )

    finally:

        try:
            loop.remove_reader(fd)
        except Exception:
            pass


# ============================================================
# Session cleanup
# ============================================================


async def _cleanup_session_async(session):

    try:

        if session.child_pid:

            try:

                pgid = os.getpgid(
                    session.child_pid
                )

                os.killpg(
                    pgid,
                    signal.SIGKILL,
                )

            except Exception:

                try:
                    os.kill(
                        session.child_pid,
                        signal.SIGKILL,
                    )
                except Exception:
                    pass

            try:
                os.waitpid(
                    session.child_pid,
                    os.WNOHANG,
                )
            except ChildProcessError:
                pass

        if session.master_fd is not None:

            try:
                os.close(
                    session.master_fd
                )
            except Exception:
                pass

        session.master_fd = None
        session.child_pid = None

    except Exception:

        log.exception(
            "[cleanup_session] %s",
            session.session_id,
        )


async def _cleanup_stale_sessions():

    now = time.time()

    async with _session_lock:

        stale_ids = []

        for sid, session in list(
            sessions.items()
        ):

            if not session.is_alive():

                stale_ids.append(sid)

                continue

            if (
                now - session.last_active
                > SESSION_IDLE_TIMEOUT
            ):

                stale_ids.append(sid)

        for sid in stale_ids:

            session = sessions.pop(
                sid,
                None,
            )

            if session:

                log.info(
                    "[cleanup] closing stale session %s",
                    sid,
                )

                session.close()


# ============================================================
# Start interactive PTY session
# ============================================================


async def _create_pty_session(
    command,
    session_id,
):

    session = PTYSession(
        session_id=session_id,
        command=command,
    )

    master_fd = None
    slave_fd = None

    try:

        master_fd, slave_fd = pty.openpty()

        _set_nonblocking(
            master_fd
        )

        child_pid = os.fork()

        if child_pid == 0:

            try:

                os.setsid()

                os.close(
                    master_fd
                )

                os.dup2(
                    slave_fd,
                    0,
                )

                os.dup2(
                    slave_fd,
                    1,
                )

                os.dup2(
                    slave_fd,
                    2,
                )

                if slave_fd > 2:
                    os.close(slave_fd)

                packed = struct.pack(
                    "HHHH",
                    80,
                    24,
                    0,
                    0,
                )

                fcntl.ioctl(
                    0,
                    termios.TIOCSWINSZ,
                    packed,
                )

                parts = shlex.split(
                    command
                )

                if not parts:
                    os._exit(1)

                os.execvp(
                    parts[0],
                    parts,
                )

            except Exception:

                os._exit(127)

        os.close(slave_fd)
        slave_fd = None

        session.master_fd = master_fd
        session.child_pid = child_pid

        session._reader_task = asyncio.create_task(
            _reader_loop(session)
        )

        return session

    except Exception:

        if slave_fd is not None:

            try:
                os.close(slave_fd)
            except Exception:
                pass

        if master_fd is not None:

            try:
                os.close(master_fd)
            except Exception:
                pass

        session.close()

        raise


# ============================================================
# Start interactive session
# ============================================================


@mcp.tool()
async def start_session(
    command: str = "/bin/bash",
    session_id: str | None = None,
):

    await _cleanup_stale_sessions()

    sid = session_id or uuid.uuid4().hex[:8]

    async with _session_lock:

        if (
            sid in sessions
            and sessions[sid].is_alive()
        ):

            return json.dumps(
                {
                    "error": (
                        f"Session '{sid}' "
                        "already exists"
                    )
                }
            )

        try:

            session = await _create_pty_session(
                command,
                sid,
            )

            sessions[sid] = session

            log.info(
                "[start_session] id=%s command=%s pid=%s",
                sid,
                command,
                session.child_pid,
            )

            return json.dumps(
                {
                    "session_id": sid,
                    "pid": session.child_pid,
                    "command": command,
                    "status": "started",
                }
            )

        except Exception as e:

            log.exception(
                "[start_session] error"
            )

            return json.dumps(
                {
                    "error": str(e)
                }
            )


# ============================================================
# Send input
# ============================================================


@mcp.tool()
async def send_input(
    session_id: str,
    text: str,
):

    async with _session_lock:

        session = sessions.get(
            session_id
        )

        if (
            not session
            or not session.is_alive()
        ):

            return json.dumps(
                {
                    "error": (
                        f"Session '{session_id}' "
                        "not found or not alive"
                    )
                }
            )

        try:

            if not text.endswith("\n"):
                text += "\n"

            data = text.encode()

            os.write(
                session.master_fd,
                data,
            )

            session.last_active = time.time()

            log.info(
                "[send_input] session=%s bytes=%s",
                session_id,
                len(data),
            )

            return json.dumps(
                {
                    "status": "sent",
                    "bytes": len(data),
                }
            )

        except Exception as e:

            log.exception(
                "[send_input] error"
            )

            return json.dumps(
                {
                    "error": str(e)
                }
            )


# ============================================================
# Read PTY output
# ============================================================


@mcp.tool()
async def read_output(
    session_id: str,
    timeout: float = 2.0,
):

    async with _session_lock:

        session = sessions.get(
            session_id
        )

        if not session:

            return json.dumps(
                {
                    "error": (
                        f"Session '{session_id}' "
                        "not found"
                    )
                }
            )

        if (
            session.closed
            and not session.output_buffer
        ):

            return json.dumps(
                {
                    "output": "",
                    "has_more": False,
                    "is_alive": False,
                }
            )

    try:
        timeout = float(timeout)
    except (ValueError, TypeError):
        timeout = 2.0

    deadline = (
        time.time()
        + max(timeout, 0)
    )

    while time.time() < deadline:

        if session.output_buffer:
            break

        if session.closed:
            break

        await asyncio.sleep(0.05)

    async with _session_lock:

        output = session.output_buffer

        session.output_buffer = ""

        is_alive = session.is_alive()

        if len(output) > OUTPUT_MAX_BYTES:

            output = output[
                -OUTPUT_MAX_BYTES:
            ]

        return json.dumps(
            {
                "output": output,
                "has_more": is_alive,
                "is_alive": is_alive,
            }
        )


# ============================================================
# List sessions
# ============================================================


@mcp.tool()
async def list_sessions():

    await _cleanup_stale_sessions()

    active = []

    async with _session_lock:

        for sid, session in list(
            sessions.items()
        ):

            if session.is_alive():

                idle_secs = int(
                    time.time()
                    - session.last_active
                )

                active.append(
                    {
                        "session_id": sid,
                        "command": session.command,
                        "idle_seconds": idle_secs,
                    }
                )

    return json.dumps(
        {
            "sessions": active,
            "count": len(active),
        }
    )


# ============================================================
# Close session
# ============================================================


@mcp.tool()
async def close_session(
    session_id: str,
):

    async with _session_lock:

        session = sessions.pop(
            session_id,
            None,
        )

        if not session:

            return json.dumps(
                {
                    "error": (
                        f"Session '{session_id}' "
                        "not found"
                    )
                }
            )

        session.close()

        log.info(
            "[close_session] closed session %s",
            session_id,
        )

        return json.dumps(
            {
                "status": "closed",
                "session_id": session_id,
            }
        )


# ============================================================
# Start background process
# ============================================================


@mcp.tool()
async def start_background(
    command: str,
):

    await _cleanup_stale_sessions()

    sid = uuid.uuid4().hex[:8]

    async with _session_lock:

        try:

            session = await _create_pty_session(
                command,
                sid,
            )

            sessions[sid] = session

            log.info(
                "[start_background] id=%s command=%s pid=%s",
                sid,
                command,
                session.child_pid,
            )

            return json.dumps(
                {
                    "session_id": sid,
                    "pid": session.child_pid,
                    "command": command,
                    "status": "started",
                }
            )

        except Exception as e:

            log.exception(
                "[start_background] error"
            )

            return json.dumps(
                {
                    "error": str(e)
                }
            )


# ============================================================
# File operations
# ============================================================


@mcp.tool()
async def write_file(
    path: str,
    content: str,
):

    """
    Write content directly to a file on the server.
    Bypasses shell encoding issues.
    """

    try:

        dirname = os.path.dirname(
            path
        )

        if dirname:

            os.makedirs(
                dirname,
                exist_ok=True,
            )

        with open(
            path,
            "w",
            encoding="utf-8",
        ) as f:

            f.write(content)

        size = os.path.getsize(
            path
        )

        log.info(
            "[write_file] written %s bytes to %s",
            size,
            path,
        )

        return json.dumps(
            {
                "status": "ok",
                "path": path,
                "bytes": size,
            }
        )

    except Exception as e:

        log.exception(
            "[write_file] error"
        )

        return json.dumps(
            {
                "status": "error",
                "error": str(e),
            }
        )


@mcp.tool()
async def read_file(
    path: str,
):

    """
    Read a UTF-8 text file from the server.
    """

    try:

        with open(
            path,
            "r",
            encoding="utf-8",
        ) as f:

            content = f.read()

        log.info(
            "[read_file] read %s bytes from %s",
            len(content),
            path,
        )

        return json.dumps(
            {
                "status": "ok",
                "content": content,
                "bytes": len(content),
            }
        )

    except Exception as e:

        log.exception(
            "[read_file] error"
        )

        return json.dumps(
            {
                "status": "error",
                "error": str(e),
            }
        )
        # ============================================================
# Starlette lifespan
# ============================================================

@contextlib.asynccontextmanager
async def lifespan(app):
    log.info("Starting MCP session manager")

    async with mcp.session_manager.run():
        log.info("MCP session manager started")
        yield

    log.info("MCP session manager stopped")


# ============================================================
# HTTP application
# ============================================================

async def http_app(
    scope,
    receive,
    send,
):
    if scope["type"] != "http":
        response = Response(
            "Unsupported protocol",
            status_code=400,
        )

        await response(
            scope,
            receive,
            send,
        )

        return

    path = scope.get(
        "path",
        "/",
    )

    # --------------------------------------------------------
    # Health check
    # --------------------------------------------------------

    if path == "/health":
        response = Response(
            "OK",
            status_code=200,
            media_type="text/plain",
        )

        await response(
            scope,
            receive,
            send,
        )

        return

    # --------------------------------------------------------
    # Legacy SSE
    #
    # /sse
    # /messages
    # --------------------------------------------------------

    if (
        path.startswith("/sse")
        or path.startswith("/messages")
    ):
        sse_app = mcp.sse_app()

        await sse_app(
            scope,
            receive,
            send,
        )

        return

    # --------------------------------------------------------
    # MCP Streamable HTTP
    #
    # /mcp
    # --------------------------------------------------------

    if path.startswith("/mcp"):
        stream_app = mcp.streamable_http_app()

        await stream_app(
            scope,
            receive,
            send,
        )

        return

    # --------------------------------------------------------
    # 404
    # --------------------------------------------------------

    response = Response(
        "Not Found",
        status_code=404,
    )

    await response(
        scope,
        receive,
        send,
    )


# ============================================================
# Host Starlette application
# ============================================================

app = Starlette(
    lifespan=lifespan,
)


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":

    log.info(
        "Starting MCP Shell Server v2 "
        "on %s:%s",
        LISTEN_HOST,
        LISTEN_PORT,
    )

    print(
        "MCP Shell Server v2 starting "
        f"on {LISTEN_HOST}:{LISTEN_PORT}"
    )

    print(
        "SSE: /sse"
    )

    print(
        "Messages: /messages"
    )

    print(
        "Streamable HTTP: /mcp"
    )

    print(
        "Health: /health"
    )

    # --------------------------------------------------------
    # Build transport applications once.
    #
    # IMPORTANT:
    #
    # mcp.streamable_http_app() depends on
    # mcp.session_manager.run().
    #
    # The Starlette lifespan above starts that manager.
    # --------------------------------------------------------

    sse_app = mcp.sse_app()

    stream_app = mcp.streamable_http_app()

    # Keep references alive for the dispatcher.

    app.state.sse_app = sse_app

    app.state.stream_app = stream_app

    # --------------------------------------------------------
    # Dispatcher
    # --------------------------------------------------------

    async def dispatcher(scope, receive, send):
        if scope["type"] != "http":
            return await http_app(scope, receive, send)
    
        path = scope.get("path", "/")
    
        # ---- Debug: log MCP JSON-RPC requests ----
        if path.startswith("/mcp"):
            original_receive = receive
            body_parts = []

            async def debug_receive():
                message = await original_receive()

                if message["type"] == "http.request":
                    body = message.get("body", b"")
                    if body:
                        body_parts.append(body)

                    if not message.get("more_body", False):
                        try:
                            import json

                            raw_body = b"".join(body_parts)
                            data = json.loads(raw_body)

                            log.info(
                                "MCP DEBUG: method=%s id=%s",
                                data.get("method"),
                                data.get("id"),
                            )

                        except Exception:
                            log.info(
                                "MCP DEBUG: non-JSON body (%d bytes)",
                                sum(len(x) for x in body_parts),
                            )

                return message

            receive = debug_receive
    
        if path.startswith("/sse") or path.startswith("/messages"):
            await app.state.sse_app(scope, receive, send)
            return
    
        if path.startswith("/mcp"):
            await app.state.stream_app(scope, receive, send)
            return
    
        if path == "/health":
            response = Response("OK", status_code=200, media_type="text/plain")
            await response(scope, receive, send)
            return
    
        response = Response("Not Found", status_code=404)
        await response(scope, receive, send)

    # --------------------------------------------------------
    # IMPORTANT FIX
    #
    # Do NOT run:
    #
    #     uvicorn.run(dispatcher, ...)
    #
    # because that bypasses Starlette's lifespan.
    #
    # Instead, mount the dispatcher under the Starlette
    # application and run the Starlette application itself.
    # --------------------------------------------------------

    app.mount(
        "/",
        dispatcher,
    )

    uvicorn.run(
        app,
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        log_level="info",
    )
