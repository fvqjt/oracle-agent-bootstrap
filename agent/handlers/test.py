"""test handler — closed-loop connectivity check.

Returns the params as-is plus a fixed message; used to verify the whole
Oracle -> Runner pipeline without touching the real system.
"""
from . import register


@register("test")
def run(params: dict) -> dict:
    return {"echo": params, "message": "hello from runner"}