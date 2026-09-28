from __future__ import annotations

import json
import hashlib
import math
from collections import defaultdict

from .statistics import crossed_paired_bootstrap


def build_report(config, store) -> dict:
    rows = store.conn.execute("""SELECT e.task_id,e.family,e.condition,e.replica,e.status,e.retryable,e.error,e.duration_seconds,ev.score_json,
                              COALESCE(SUM(CASE WHEN r.state='rejected' THEN 0 ELSE COALESCE(r.actual_usd,r.reserved_usd) END),0) cost,
                              SUM(CASE WHEN r.state='completed' THEN 1 ELSE 0 END) model_calls,
                              SUM(CASE WHEN r.state IN ('reserved','submitted','unknown_outcome') THEN 1 ELSE 0 END) uncertain_requests
                              FROM episodes e LEFT JOIN evaluations ev ON ev.episode_id=e.episode_id
                              LEFT JOIN requests r ON r.episode_id=e.episode_id WHERE e.campaign=? GROUP BY e.episode_id""", (config.campaign,)).fetchall()
    groups = defaultdict(lambda: {"planned": 0, "completed": 0, "execution_failed": 0, "pending": 0, "blocked": 0, "scored": [], "costs": [], "terminal_costs": [], "terminal_seconds": [], "terminal_calls": [], "uncertain_requests": 0})
    creation = defaultdict(lambda: {"episodes": 0, "completed": 0, "failed": 0, "total_usd": 0.0})
    creation_costs = defaultdict(float)
    for row in rows:
        if row["family"] == "integration": continue
        if row["task_id"].startswith("creation"):
            key = f"{row['family']}:{row['condition']}:{row['replica']}"
            item = creation[key]; item["episodes"] += 1; item["completed"] += row["status"] == "completed"; item["failed"] += row["status"] == "failed"; item["total_usd"] += row["cost"]
            creation_costs[(row["family"], row["condition"], row["replica"])] += float(row["cost"])
            continue
        group = groups[f"{row['family']}:{row['condition']}"]
        group["planned"] += 1; group["costs"].append(row["cost"]); group["uncertain_requests"] += row["uncertain_requests"] or 0
        terminal = (row["status"] == "completed" and row["score_json"] is not None) or (row["status"] == "failed" and not row["retryable"])
        if terminal:
            group["terminal_costs"].append(float(row["cost"]))
            group["terminal_seconds"].append(float(row["duration_seconds"]))
            group["terminal_calls"].append(int(row["model_calls"]))
        if row["status"] == "completed" and row["score_json"]:
            group["completed"] += 1; group["scored"].append(float(json.loads(row["score_json"])["primary"]))
        elif row["status"] == "failed" and not row["retryable"]: group["execution_failed"] += 1
        elif row["status"] == "blocked": group["blocked"] += 1
        else: group["pending"] += 1
        group.setdefault("terminal", 0); group["terminal"] += terminal
    binding = store.study_binding(config.campaign)
    terminal_total = sum(group["terminal"] for group in groups.values())
    complete = bool(binding and terminal_total == binding["planned_test_episodes"] and sum(group["blocked"] for group in groups.values()) == 0)
    output = {"campaign": config.campaign, "analysis_status": "complete" if complete else "provisional", "planned_test_episodes": binding["planned_test_episodes"] if binding else None, "groups": {}, "skill_creation": dict(creation), "status": store.campaign_status(config.campaign)}
    for name, group in sorted(groups.items()):
        scored_total = sum(group["scored"])
        output["groups"][name] = {
            "planned": group["planned"], "completed_and_scored": group["completed"], "execution_failed": group["execution_failed"], "blocked": group["blocked"], "pending_or_unscored": group["pending"], "terminal": group["terminal"],
            "mean_primary_among_scored": scored_total / group["completed"] if group["completed"] else None,
            "primary_on_terminal_episodes": scored_total / group["terminal"] if group["terminal"] else None,
            "primary_with_failures_as_zero": scored_total / group["terminal"] if group["terminal"] else None,
            "mean_usd_per_planned_episode": sum(group["costs"]) / group["planned"] if group["planned"] else 0,
            "mean_usd_per_terminal_episode": sum(group["terminal_costs"]) / group["terminal"] if group["terminal"] else None,
            "mean_seconds_per_terminal_episode": sum(group["terminal_seconds"]) / group["terminal"] if group["terminal"] else None,
            "mean_model_calls_per_terminal_episode": sum(group["terminal_calls"]) / group["terminal"] if group["terminal"] else None,
            "uncertain_provider_requests": group["uncertain_requests"],
        }
    by_cell = {}
    for row in rows:
        if row["task_id"].startswith("creation") or row["family"] == "integration": continue
        terminal = (row["status"] == "completed" and row["score_json"] is not None) or (row["status"] == "failed" and not row["retryable"])
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
            grouped[family][task_id].append((replica, ((action["score"], action["cost"]), (other["score"], other["cost"]))))
            replicas[family].add(replica)
        for family, tasks in grouped.items():
            salt = int(hashlib.sha256(f"{config.campaign}|{family}|{baseline}".encode()).hexdigest()[:8], 16)
            quality_cells = {(task, replica): (action[0], other[0]) for task, pairs in tasks.items() for replica, (action, other) in pairs}
            cost_cells = {(task, replica): (action[1], other[1]) for task, pairs in tasks.items() for replica, (action, other) in pairs}
            mean_runtime_delta = sum(action[1] - other[1] for pairs in tasks.values() for _, (action, other) in pairs) / sum(len(pairs) for pairs in tasks.values())
            baseline_creation = "skill_script" if baseline == "skill_script" else None
            per_replica_creation = [creation_costs[(family, "action", replica)] - (creation_costs[(family, baseline_creation, replica)] if baseline_creation else 0.0) for replica in replicas[family]]
            mean_creation_delta = sum(per_replica_creation) / len(per_replica_creation) if per_replica_creation else 0.0
            break_even = None if mean_runtime_delta >= 0 else max(0, int(math.ceil(mean_creation_delta / -mean_runtime_delta)))
            quality = crossed_paired_bootstrap(quality_cells, salt)
            usd = crossed_paired_bootstrap(cost_cells, salt + 1)
            inferential = complete and quality["n_tasks"] >= 10 and quality["n_replicas"] >= 3 and not excluded[family]
            if not inferential:
                quality["ci95"] = None; usd["ci95"] = None
            comparisons[f"{family}:action_minus_{baseline}"] = {
                "quality": quality,
                "usd": usd,
                "inferential_interval_available": inferential,
                "incomplete_pairs_excluded": excluded[family],
                "amortization": {"mean_creation_delta_usd_per_replica": mean_creation_delta, "mean_runtime_delta_usd_per_episode": mean_runtime_delta, "break_even_uses_per_package": break_even},
            }
    output["paired_comparisons"] = comparisons
    usage = store.conn.execute("""SELECT e.family,e.condition,ar.action_id,COUNT(*) invocations,
                                  SUM(CASE WHEN ar.state='completed' THEN 1 ELSE 0 END) completed
                                  FROM action_runs ar JOIN episodes e ON e.episode_id=ar.episode_id
                                  WHERE e.campaign=? AND e.family!='integration' AND e.task_id NOT LIKE 'creation%'
                                  GROUP BY e.family,e.condition,ar.action_id""", (config.campaign,)).fetchall()
    output["procedure_usage"] = [dict(row) for row in usage]
    output["pricing_configured"] = all(value > 0 for value in (config.provider.input_usd_per_million, config.provider.output_usd_per_million))
    return output
