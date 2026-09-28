from __future__ import annotations

import json
from collections import defaultdict


def build_report(config, store) -> dict:
    rows = store.conn.execute("""SELECT e.task_id,e.family,e.condition,e.replica,e.status,ev.score_json,
                              COALESCE(SUM(r.actual_usd),0) cost FROM episodes e
                              LEFT JOIN evaluations ev ON ev.episode_id=e.episode_id
                              LEFT JOIN requests r ON r.episode_id=e.episode_id
                              WHERE e.campaign=? GROUP BY e.episode_id""", (config.campaign,)).fetchall()
    groups = defaultdict(lambda: {"episodes": 0, "completed": 0, "scores": [], "costs": []})
    creation = defaultdict(lambda: {"episodes": 0, "completed": 0, "costs": []})
    for row in rows:
        if row["task_id"].startswith("creation-"):
            c = creation[row["family"]]; c["episodes"] += 1; c["completed"] += row["status"] == "completed"; c["costs"].append(row["cost"]); continue
        group = groups[f"{row['family']}:{row['condition']}"]; group["episodes"] += 1; group["completed"] += row["status"] == "completed"; group["costs"].append(row["cost"])
        if row["score_json"]: group["scores"].append(json.loads(row["score_json"]).get("primary"))
    output = {"campaign": config.campaign, "groups": {}}
    for name, group in sorted(groups.items()):
        scores = [x for x in group["scores"] if isinstance(x, (int, float))]
        output["groups"][name] = {"episodes": group["episodes"], "completed": group["completed"], "mean_primary": sum(scores) / len(scores) if scores else None, "mean_usd": sum(group["costs"]) / len(group["costs"]) if group["costs"] else 0}
    output["skill_creation"] = {family: {"episodes": item["episodes"], "completed": item["completed"], "total_usd": sum(item["costs"])} for family, item in sorted(creation.items())}
    output["status"] = store.campaign_status(config.campaign)
    return output
