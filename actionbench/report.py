from __future__ import annotations

import json
import hashlib
import math
from collections import defaultdict

from .statistics import clustered_paired_bootstrap


def build_report(config, store) -> dict:
    rows = store.conn.execute("""SELECT e.task_id,e.family,e.condition,e.replica,e.status,e.retryable,e.error,ev.score_json,
                              COALESCE(SUM(COALESCE(r.actual_usd,r.reserved_usd)),0) cost
                              FROM episodes e LEFT JOIN evaluations ev ON ev.episode_id=e.episode_id
                              LEFT JOIN requests r ON r.episode_id=e.episode_id WHERE e.campaign=? GROUP BY e.episode_id""", (config.campaign,)).fetchall()
    groups = defaultdict(lambda: {"planned": 0, "completed": 0, "execution_failed": 0, "pending": 0, "scored": [], "costs": []})
    creation = defaultdict(lambda: {"episodes": 0, "completed": 0, "failed": 0, "total_usd": 0.0})
    creation_costs = defaultdict(float)
    for row in rows:
        if row["task_id"].startswith("creation"):
            key = f"{row['family']}:{row['condition']}:{row['replica']}"
            item = creation[key]; item["episodes"] += 1; item["completed"] += row["status"] == "completed"; item["failed"] += row["status"] == "failed"; item["total_usd"] += row["cost"]
            creation_costs[(row["family"], row["condition"], row["replica"])] += float(row["cost"])
            continue
        group = groups[f"{row['family']}:{row['condition']}"]
        group["planned"] += 1; group["costs"].append(row["cost"])
        terminal = row["status"] == "completed" or (row["status"] == "failed" and not row["retryable"])
        if row["status"] == "completed" and row["score_json"]:
            group["completed"] += 1; group["scored"].append(float(json.loads(row["score_json"])["primary"]))
        elif row["status"] == "failed": group["execution_failed"] += 1
        else: group["pending"] += 1
        group.setdefault("terminal", 0); group["terminal"] += terminal
    output = {"campaign": config.campaign, "groups": {}, "skill_creation": dict(creation), "status": store.campaign_status(config.campaign)}
    for name, group in sorted(groups.items()):
        scored_total = sum(group["scored"])
        output["groups"][name] = {
            "planned": group["planned"], "completed_and_scored": group["completed"], "execution_failed": group["execution_failed"], "pending_or_unscored": group["pending"],
            "mean_primary_among_scored": scored_total / group["completed"] if group["completed"] else None,
            "primary_on_terminal_episodes": scored_total / group["terminal"] if group["terminal"] else None,
            "primary_with_failures_as_zero": scored_total / group["terminal"] if group["terminal"] else None,
            "mean_usd_per_planned_episode": sum(group["costs"]) / group["planned"] if group["planned"] else 0,
        }
    by_cell = {}
    for row in rows:
        if row["task_id"].startswith("creation"): continue
        terminal = row["status"] == "completed" or (row["status"] == "failed" and not row["retryable"])
        score = 0.0
        if row["status"] == "completed" and row["score_json"]: score = float(json.loads(row["score_json"])["primary"])
        by_cell[(row["family"], row["task_id"], row["replica"], row["condition"])] = {"terminal": terminal, "score": score, "cost": float(row["cost"])}
    comparisons = {}
    for baseline in ("skill", "skill_script", "improvised"):
        grouped = defaultdict(lambda: defaultdict(list)); excluded = defaultdict(int); replicas = defaultdict(set)
        for (family, task_id, replica, condition), action in by_cell.items():
            if condition != "action": continue
            other = by_cell.get((family, task_id, replica, baseline))
            if not other or not action["terminal"] or not other["terminal"]:
                excluded[family] += 1; continue
            grouped[family][task_id].append(((action["score"], action["cost"]), (other["score"], other["cost"])))
            replicas[family].add(replica)
        for family, tasks in grouped.items():
            salt = int(hashlib.sha256(f"{config.campaign}|{family}|{baseline}".encode()).hexdigest()[:8], 16)
            quality_clusters = {task: [(a[0], b[0]) for a, b in pairs] for task, pairs in tasks.items()}
            cost_clusters = {task: [(a[1], b[1]) for a, b in pairs] for task, pairs in tasks.items()}
            mean_runtime_delta = sum(a[1] - b[1] for pairs in tasks.values() for a, b in pairs) / sum(len(pairs) for pairs in tasks.values())
            per_replica_creation = [creation_costs[(family, "action", replica)] - creation_costs[(family, baseline if baseline != "improvised" else "skill", replica)] for replica in replicas[family]]
            mean_creation_delta = sum(per_replica_creation) / len(per_replica_creation) if per_replica_creation else 0.0
            break_even = None if mean_runtime_delta >= 0 else max(0, int(math.ceil(mean_creation_delta / -mean_runtime_delta)))
            comparisons[f"{family}:action_minus_{baseline}"] = {
                "quality": clustered_paired_bootstrap(quality_clusters, salt),
                "usd": clustered_paired_bootstrap(cost_clusters, salt + 1),
                "incomplete_pairs_excluded": excluded[family],
                "amortization": {"mean_creation_delta_usd_per_replica": mean_creation_delta, "mean_runtime_delta_usd_per_episode": mean_runtime_delta, "break_even_uses_per_package": break_even},
            }
    output["paired_comparisons"] = comparisons
    return output
