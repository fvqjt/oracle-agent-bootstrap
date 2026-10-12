"""PTY-based interactive exec handler for WebSocket sessions.

Creates a pseudo-terminal, spawns the command bound to it,
streams output via WebSocket, accepts stdin/signal/resize via WebSocket.
"""
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
import threading
import time


def handle_exec_pty(msg: dict, ws_client):
    """Handle exec_start with pty=true.

    msg: {type: exec_start, session_id, command, pty, timeout_sec}
    ws_client: WSClient instance with send_json()
    """
    session_id = msg.get("session_id", "")
    command = msg.get("command", ["bash"])
    timeout_sec = msg.get("timeout_sec", 300)

    master_fd, slave_fd = pty.openpty()

    # Set initial window size
    try:
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    except Exception:
        pass

    try:
        proc = subprocess.Popen(
            [str(c) for c in command],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
    except Exception as e:
        ws_client.send_json({"type": "error", "session_id": session_id, "message": f"spawn failed: {e}"})
        os.close(master_fd)
        os.close(slave_fd)
        return

    os.close(slave_fd)  # parent doesn't need slave

    # Session state for this PTY
    state = {"stop": False, "proc": proc, "master_fd": master_fd}

    # Output reader thread: master_fd -> WS output frames
    def _read_output():
        while not state["stop"]:
            try:
                data = os.read(master_fd, 4096)
            except (OSError, IOError):
                break
            if not data:
                break
            try:
                ws_client.send_json({
                    "type": "output",
                    "session_id": session_id,
                    "stream": "stdout",
                    "data": data.decode("utf-8", errors="replace"),
                })
            except Exception:
                break
        # Process exited
        if not state["stop"]:
            try:
                exit_code = proc.wait()
            except Exception:
                exit_code = -1
            try:
                ws_client.send_json({"type": "exit", "session_id": session_id, "code": exit_code})
            except Exception:
                pass

    reader = threading.Thread(target=_read_output, daemon=True)
    reader.start()

    # Timeout timer
    def _timeout():
        time.sleep(timeout_sec)
        if not state["stop"]:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                time.sleep(5)
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass

    timer = threading.Thread(target=_timeout, daemon=True)
    timer.start()

    # Return state so caller can handle stdin/signal/resize messages
    return state


def handle_pty_input(state: dict, msg: dict):
    """Handle stdin/signal/resize for an active PTY session."""
    if not state:
        return
    msg_type = msg.get("type")
    master_fd = state.get("master_fd")
    proc = state.get("proc")

    if msg_type == "stdin" and master_fd is not None:
        try:
            os.write(master_fd, msg.get("data", "").encode())
        except (OSError, IOError):
            pass

    elif msg_type == "resize" and master_fd is not None:
        rows = msg.get("rows", 24)
        cols = msg.get("cols", 80)
        try:
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    elif msg_type == "signal" and proc:
        sig = msg.get("signal", "SIGINT")
        if sig == "SIGINT" and master_fd is not None:
            try:
                os.write(master_fd, b"\x03")  # Ctrl-C
            except (OSError, IOError):
                pass
        elif sig in ("SIGTERM", "SIGKILL"):
            try:
                os.killpg(proc.pid, getattr(signal, sig))
            except Exception:
                pass

    elif msg_type == "exec_end":
        state["stop"] = True
        if proc:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                pass
        if master_fd is not None:
            try:
                os.close(master_fd)
            except Exception:
                pass
