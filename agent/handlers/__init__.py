"""Handler registry for Oracle Agent task types.

A handler is a callable `fn(params: dict) -> dict` returning the task output.
It may raise:
  - HandlerError(message, output=None, status="failed")  -> status failed
  - HandlerTimeout(message, output=None)                 -> status timeout

Extend by adding a new module below and decorating its entry with @register.
"""


class HandlerError(Exception):
    def __init__(self, message, output=None, status="failed"):
        super().__init__(message)
        self.message = message
        self.output = output
        self.status = status


class HandlerTimeout(HandlerError):
    def __init__(self, message, output=None):
        super().__init__(message, output=output, status="timeout")


HANDLERS = {}


def register(name):
    def deco(fn):
        HANDLERS[name] = fn
        return fn

    return deco


# Import handlers after the registry primitives exist (avoids circular import).
from .exec import run as exec_run  # noqa: E402
from .exec_pty import handle_exec_pty as exec_pty_run  # noqa: E402
from .shutdown import run as shutdown_run  # noqa: E402
from .test import run as test_run  # noqa: E402

HANDLERS["test"] = test_run
HANDLERS["exec"] = exec_run
HANDLERS["shutdown"] = shutdown_run
HANDLERS["exec_pty"] = exec_pty_run