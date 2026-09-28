from __future__ import annotations

import json
from collections import defaultdict


def build_report(config, store) -> dict:
    rows = store.conn.execute("""SELECT e.task_id,e.family,e.condition,e.status,e.error,ev.score_json,
                              COALESCE(SUM(COALESCE(r.actual_usd,r.reserved_usd)),0) cost
                              FROM episodes e LEFT JOIN evaluations ev ON ev.episode_id=e.episode_id
                              LEFT JOIN requests r ON r.episode_id=e.episode_id WHERE e.campaign=? GROUP BY e.episode_id""", (config.campaign,)).fetchall()
    groups = defaultdict(lambda: {"planned": 0, "completed": 0, "execution_failed": 0, "pending": 0, "scored": [], "costs": []})
    creation = defaultdict(lambda: {"episodes": 0, "completed": 0, "failed": 0, "total_usd": 0.0})
    for row in rows:
        if row["task_id"].startswith("creation:"):
            item = creation[row["family"]]; item["episodes"] += 1; item["completed"] += row["status"] == "completed"; item["failed"] += row["status"] == "failed"; item["total_usd"] += row["cost"]; continue
        group = groups[f"{row['family']}:{row['condition']}"]
        group["planned"] += 1; group["costs"].append(row["cost"])
        if row["status"] == "completed" and row["score_json"]:
            group["completed"] += 1; group["scored"].append(float(json.loads(row["score_json"])["primary"]))
        elif row["status"] == "failed": group["execution_failed"] += 1
        else: group["pending"] += 1
    output = {"campaign": config.campaign, "groups": {}, "skill_creation": dict(creation), "status": store.campaign_status(config.campaign)}
    for name, group in sorted(groups.items()):
        scored_total = sum(group["scored"])
        output["groups"][name] = {
            "planned": group["planned"], "completed_and_scored": group["completed"], "execution_failed": group["execution_failed"], "pending_or_unscored": group["pending"],
            "mean_primary_among_scored": scored_total / group["completed"] if group["completed"] else None,
            "primary_with_failures_as_zero": scored_total / group["planned"] if group["planned"] else None,
            "mean_usd_per_planned_episode": sum(group["costs"]) / group["planned"] if group["planned"] else 0,
        }
    return output
