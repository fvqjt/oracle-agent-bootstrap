"""Report bot status to Oracle via oracle-agent's existing report endpoint.

Reads agent_id + token from the agent's state.json, and oracle_url from
the agent's config.json.  No modification to oracle-agent code is needed.
"""
import asyncio
import json
import logging
import urllib.request
from pathlib import Path

from state_mgr import StateManager

log = logging.getLogger("tg.status")


class StatusReporter:
    def __init__(self, state_mgr: StateManager, agent_dir: str, interval_sec: int = 60):
        self._state_mgr = state_mgr
        self._agent_dir = Path(agent_dir)
        self._interval = interval_sec
        self._agent_id = None
        self._token = None
        self._oracle_url = None

    def _load_agent_info(self) -> bool:
        """Read agent_id, token, oracle_url from the agent's local files."""
        state_path = self._agent_dir / "state.json"
        config_path = self._agent_dir / "config.json"
        try:
            state = json.loads(state_path.read_text())
            self._agent_id = state["agent_id"]
            self._token = state["token"]
            config = json.loads(config_path.read_text())
            self._oracle_url = config["oracle_url"].rstrip("/")
            return True
        except Exception as e:
            log.warning("cannot read agent info: %s", e)
            return False

    def _report_sync(self):
        """Synchronous report call (runs in thread via asyncio.to_thread)."""
        state = self._state_mgr.current_state()
        payload = json.dumps({
            "agent_id": self._agent_id,
            "event": "bot_status",
            "data": {
                "bot_status": state.value,
                "active_tasks": self._state_mgr.active_tasks,
                "idle_seconds": round(self._state_mgr.idle_seconds(), 1),
            },
        }).encode()
        req = urllib.request.Request(
            f"{self._oracle_url}/api/v1/agent/report",
            data=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        urllib.request.urlopen(req, timeout=10)

    async def report_once(self):
        """Send a single status report."""
        if not self._agent_id:
            if not self._load_agent_info():
                return
        try:
            await asyncio.to_thread(self._report_sync)
        except Exception as e:
            log.warning("status report failed: %s", e)

    async def run_forever(self):
        """Background loop: report status every interval seconds."""
        while True:
            await asyncio.sleep(self._interval)
            await self.report_once()
