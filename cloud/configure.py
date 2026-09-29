"""Freeze a Vercel Sandbox campaign config without storing the provider secret."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path


def price(name: str, *, positive: bool = True) -> float:
    raw = os.environ.get(name, "")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"Set {name} to a verified USD per million token price") from exc
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise ValueError(f"Set {name} to a valid {'positive' if positive else 'nonnegative'} price")
    return value


def main() -> None:
    model = os.environ.get("AB_MODEL", "")
    if not model or model == "SET_A_REAL_MODEL":
        raise ValueError("Set AB_MODEL to a real provider model")
    campaign = os.environ.get("AB_CAMPAIGN", "pilot-001")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", campaign):
        raise ValueError("AB_CAMPAIGN has an invalid name")
    source = Path("config.example.json")
    data = json.loads(source.read_text())
    data["campaign"] = campaign
    data["provider"]["model"] = model
    data["provider"]["input_usd_per_million"] = price("AB_INPUT_USD_PER_MILLION")
    data["provider"]["cached_input_usd_per_million"] = price("AB_CACHED_INPUT_USD_PER_MILLION", positive=False) if os.environ.get("AB_CACHED_INPUT_USD_PER_MILLION") else 0.0
    data["provider"]["output_usd_per_million"] = price("AB_OUTPUT_USD_PER_MILLION")
    if os.environ.get("AB_BUDGET_USD"):
        data["budget"]["usd"] = price("AB_BUDGET_USD")
    target = Path("experiment.json")
    if target.exists():
        if json.loads(target.read_text()) != data:
            raise ValueError("Campaign config is already frozen with different values; use a new sandbox/campaign")
        return
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.chmod(0o600)
    temp.replace(target)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
