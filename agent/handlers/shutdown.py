"""shutdown handler — power off the runner.

Oracle 下发生命周期结束指令。默认延迟 1 分钟关机（先给 agent 时间把 result 回报
给 Oracle，再断电）；需要立即关机传 {"delay_sec": 0}。
"""
import subprocess

from . import HandlerError, register

SHUTDOWN_BIN = "/usr/sbin/shutdown"


@register("shutdown")
def run(params: dict) -> dict:
    try:
        delay_sec = max(0, int(params.get("delay_sec", 60)))
    except (TypeError, ValueError):
        delay_sec = 60
    # shutdown -h +N 的单位是分钟；60 秒以内的延迟用 sleep 前置
    if delay_sec == 0:
        cmd = [SHUTDOWN_BIN, "-h", "now"]
    else:
        minutes = max(1, (delay_sec + 59) // 60)
        cmd = [SHUTDOWN_BIN, "-h", f"+{minutes}"]
    try:
        subprocess.run(cmd, check=False, timeout=10)
    except FileNotFoundError:
        raise HandlerError(f"{SHUTDOWN_BIN} not found")
    except Exception as e:  # shutdown 已发出即可，剩余异常不阻塞回报
        return {"status": "shutdown-issued", "note": str(e), "delay_sec": delay_sec}
    return {"status": "shutdown-issued", "delay_sec": delay_sec}