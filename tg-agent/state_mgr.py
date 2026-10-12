"""Bot state management: busy / active / idle, using monotonic clock."""
import time
from enum import Enum


class BotState(Enum):
    BUSY = "busy"
    ACTIVE = "active"
    IDLE = "idle"


class StateManager:
    """Tracks user interaction time and running tasks to compute bot state.

    Uses time.monotonic() to avoid issues from system clock adjustments
    (NTP sync, manual changes).
    """

    def __init__(self, idle_threshold_sec: int = 600):
        self._last_activity = time.monotonic()
        self._active_tasks = 0
        self._idle_threshold = idle_threshold_sec

    def on_user_interaction(self):
        """Call when any user interaction happens (command received, completed)."""
        self._last_activity = time.monotonic()

    def task_start(self):
        self._active_tasks += 1

    def task_end(self):
        self._active_tasks = max(0, self._active_tasks - 1)

    @property
    def active_tasks(self) -> int:
        return self._active_tasks

    def current_state(self) -> BotState:
        if self._active_tasks > 0:
            return BotState.BUSY
        idle_sec = time.monotonic() - self._last_activity
        if idle_sec >= self._idle_threshold:
            return BotState.IDLE
        return BotState.ACTIVE

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity
