from __future__ import annotations

import argparse
import json
import shutil
import sys

from .config import load_config
from .errors import ActionBenchError
from .store import Store


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="actionbench")
    subs = p.add_subparsers(dest="command", required=True)
    for name in ("doctor", "live-check", "status", "datasets", "create-skills", "run", "resume", "freeze", "report"):
        child = subs.add_parser(name)
        if name == "datasets": child.add_argument("operation", choices=["prepare"])
        child.add_argument("--config", required=True)
        child.add_argument("--manifest")
        child.add_argument("--out")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            output = {"python": sys.version.split()[0], "docker": shutil.which("docker"), "config_hash": config.fingerprint, "database": str(config.db_path)}
            print(json.dumps(output, indent=2))
            return 0 if output["docker"] else 2
        store = Store(config.db_path)
        store.ensure_campaign(config)
        try:
            if args.command == "status":
                print(json.dumps(store.campaign_status(config.campaign), indent=2))
                return 0
            from .commands import dispatch
            return dispatch(args, config, store)
        finally:
            store.close()
    except ActionBenchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
