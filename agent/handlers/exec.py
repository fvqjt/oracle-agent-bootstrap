"""exec handler — run a shell command with a timeout.

params:
  command:     non-empty list, e.g. ["python3", "/opt/tasks/test.py"]
  timeout_sec: seconds (default 300)

result (success):
  {"stdout": "...", "stderr": "...", "exit_code": 0}
"""
import subprocess

from . import HandlerError, HandlerTimeout, register


@register("exec")
def run(params: dict) -> dict:
    command = params.get("command")
    if not isinstance(command, list) or not command:
        raise HandlerError("params.command must be a non-empty list")
    timeout = params.get("timeout_sec", 300)
    try:
        proc = subprocess.run(
            [str(c) for c in command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise HandlerTimeout(
            f"command timed out after {timeout}s",
            output={"stdout": e.stdout, "stderr": e.stderr},
        )
    result = {"stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode}
    if proc.returncode != 0:
        raise HandlerError(f"command exited with code {proc.returncode}", output=result)
    return result