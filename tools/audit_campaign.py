"""Read-only audit of a saved ActionBench campaign ledger.

Usage: python tools/audit_campaign.py PATH/TO/actionbench-v3.sqlite3
Optional: --evidence-dir PATH/TO/EXTRACTED/ARTIFACT
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit(db_path: Path, evidence_dir: Path | None = None) -> dict:
    db_path = db_path.resolve()
    connection = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        campaign_rows = connection.execute("SELECT campaign,status,config_hash FROM campaigns").fetchall()
        if len(campaign_rows) != 1:
            raise ValueError("Audit requires exactly one campaign per ledger")
        campaign = campaign_rows[0]["campaign"]
        rows = connection.execute("""SELECT e.task_id,e.family,e.condition,e.status,e.retryable,
            e.error,v.episode_id scored FROM episodes e LEFT JOIN evaluations v ON v.episode_id=e.episode_id
            WHERE e.campaign=?""", (campaign,)).fetchall()
        tests = [r for r in rows if r["family"] != "integration" and not r["task_id"].startswith("creation")]
        creation = [r for r in rows if r["task_id"].startswith("creation:")]
        request_rows = connection.execute("""SELECT r.state,r.actual_usd,r.reserved_usd,r.response_json,
            e.task_id,e.condition FROM requests r JOIN episodes e ON e.episode_id=r.episode_id
            WHERE e.campaign=?""", (campaign,)).fetchall()
        incomplete = Counter()
        for row in request_rows:
            if row["response_json"]:
                response = json.loads(row["response_json"])
                if response.get("status") == "incomplete":
                    incomplete[row["condition"]] += 1
        package_rows = connection.execute("SELECT condition,COUNT(*) n FROM generated_packages WHERE campaign=? GROUP BY condition", (campaign,)).fetchall()
        packages = {r["condition"]: r["n"] for r in package_rows}
        test_failures = Counter((r["condition"], r["error"] or "unspecified") for r in tests if r["status"] == "failed")
        evidence = {}
        if evidence_dir:
            for name in ("original-artifact.zip", "experiment.json", "actionbench-v3.sqlite3", "report.json", "status.json"):
                path = evidence_dir / name
                if path.is_file():
                    evidence[name] = {"sha256": sha256(path), "bytes": path.stat().st_size}
        return {
            "campaign": campaign,
            "campaign_status": campaign_rows[0]["status"],
            "config_hash": campaign_rows[0]["config_hash"],
            "ledger_sha256": sha256(db_path),
            "test_episodes": len(tests),
            "test_scored": sum(r["status"] == "completed" and r["scored"] is not None for r in tests),
            "test_failed": sum(r["status"] == "failed" for r in tests),
            "package_creation": {kind: {"attempted": sum(r["condition"] == kind for r in creation),
                                        "created": packages.get(kind, 0)} for kind in ("skill", "skill_script", "action")},
            "requests_by_state": dict(Counter(r["state"] for r in request_rows)),
            "incomplete_by_condition": dict(incomplete),
            "accounted_usd": sum((r["actual_usd"] if r["actual_usd"] is not None else r["reserved_usd"])
                                 for r in request_rows if r["state"] != "rejected"),
            "top_test_failures": [{"condition": kind, "error": error[:250], "count": count}
                                  for (kind, error), count in test_failures.most_common(20)],
            "evidence_hashes": evidence,
        }
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.ledger, args.evidence_dir), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
