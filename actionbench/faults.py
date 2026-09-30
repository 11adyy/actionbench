"""Opt-in crash points for checkpoint tests; never enabled by campaign config."""

from __future__ import annotations

import os


def checkpoint(name: str) -> None:
    if os.environ.get("AB_TEST_FAULT_POINT") == name:
        raise SystemExit(97)
