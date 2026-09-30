from __future__ import annotations

import argparse
import fcntl
import json
import shutil
import sys
from contextlib import contextmanager

from .config import load_config
from .errors import ActionBenchError
from .store import Store


@contextmanager
def campaign_lock(path):
    """Only one coordinator may mutate or resume a campaign at a time."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ActionBenchError(f"Another campaign coordinator holds {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="actionbench")
    subs = p.add_subparsers(dest="command", required=True)
    for name in ("doctor", "live-check", "status", "datasets", "images", "budget", "smoke", "integration-check", "canary", "resolve-request", "plan-sample", "create-skills", "run", "resume", "freeze", "report"):
        child = subs.add_parser(name)
        if name == "datasets": child.add_argument("operation", choices=["prepare"])
        if name == "images": child.add_argument("operation", choices=["build"])
        child.add_argument("--config", required=True)
        child.add_argument("--manifest")
        child.add_argument("--out")
        if name == "budget": child.add_argument("--usd", required=True, type=float)
        if name == "resolve-request":
            child.add_argument("--request-id", required=True)
            child.add_argument("--evidence", required=True)
            resolution = child.add_mutually_exclusive_group(required=True)
            resolution.add_argument("--response-file")
            resolution.add_argument("--confirmed-not-executed", action="store_true")
        if name == "plan-sample":
            child.add_argument("--family", required=True)
            child.add_argument("--baseline", default="skill")
            child.add_argument("--target-delta", required=True, type=float)
            child.add_argument("--target-half-width", required=True, type=float)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            output = {"python": sys.version.split()[0], "docker": shutil.which("docker"), "config_hash": config.fingerprint, "database": str(config.db_path)}
            print(json.dumps(output, indent=2))
            return 0 if output["docker"] else 2
        with campaign_lock(config.db_path):
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
