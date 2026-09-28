from __future__ import annotations

from .errors import ActionBenchError


def dispatch(args, config, store) -> int:
    raise ActionBenchError(f"Command '{args.command}' is not implemented in this repository revision")
