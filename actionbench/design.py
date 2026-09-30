"""Pilot-based planning for a separate, prospective confirmatory campaign."""

from __future__ import annotations

import json
import math

from .errors import ActionBenchError


def plan_sample(config, store, family: str, baseline: str, target_delta: float, target_half_width: float) -> dict:
    if store.campaign_status(config.campaign).get("status") != "frozen":
        raise ActionBenchError("Freeze the completed pilot before planning a new campaign from its outcomes")
    if baseline not in {"skill", "skill_script", "improvised"}:
        raise ActionBenchError("Baseline must be skill, skill_script, or improvised")
    from .report import build_report
    report = build_report(config, store)
    comparison = report["paired_comparisons"].get(f"{family}:action_minus_{baseline}")
    if report["scientific_status"] != "exploratory" or not comparison or not comparison["interpretable"]:
        raise ActionBenchError("Pilot is diagnostic or the requested contrast is not interpretable; cannot plan a confirmatory sample")
    if not 0 < target_delta <= 1 or not 0 < target_half_width <= 1:
        raise ActionBenchError("Target quality difference and interval half-width must be in (0,1]")
    rows = store.conn.execute("""SELECT e.task_id,e.replica,e.condition,e.status,e.retryable,v.score_json
        FROM episodes e LEFT JOIN evaluations v ON v.episode_id=e.episode_id
        WHERE e.campaign=? AND e.family=? AND e.condition IN ('action',?) AND e.task_id NOT LIKE 'creation%'""",
        (config.campaign, family, baseline)).fetchall()
    values = {}
    for row in rows:
        terminal = (row["status"] == "completed" and row["score_json"] is not None) or (row["status"] == "failed" and not row["retryable"])
        if not terminal: raise ActionBenchError("Pilot design requires terminal outcomes for every selected episode")
        value = float(json.loads(row["score_json"])["primary"]) if row["status"] == "completed" else 0.0
        values[(row["task_id"], row["replica"], row["condition"])] = value
    tasks = sorted({task for task, _, _ in values})
    replicas = sorted({replica for _, replica, _ in values})
    if len(tasks) < 3 or len(replicas) < 2:
        raise ActionBenchError("Need at least three tasks and two package replicas to estimate both variance components")
    if len(values) != len(tasks) * len(replicas) * 2:
        raise ActionBenchError("Pilot design needs a complete paired task-by-replica grid")
    differences = {(task, replica): values[(task, replica, "action")] - values[(task, replica, baseline)] for task in tasks for replica in replicas}
    nt, nr = len(tasks), len(replicas)
    grand = sum(differences.values()) / (nt * nr)
    task_mean = {task: sum(differences[(task, replica)] for replica in replicas) / nr for task in tasks}
    replica_mean = {replica: sum(differences[(task, replica)] for task in tasks) / nt for replica in replicas}
    ms_task = nr * sum((task_mean[task] - grand) ** 2 for task in tasks) / (nt - 1)
    ms_replica = nt * sum((replica_mean[replica] - grand) ** 2 for replica in replicas) / (nr - 1)
    ms_interaction = sum((differences[(task, replica)] - task_mean[task] - replica_mean[replica] + grand) ** 2 for task in tasks for replica in replicas) / ((nt - 1) * (nr - 1))
    task_variance = max(0.0, (ms_task - ms_interaction) / nr)
    replica_variance = max(0.0, (ms_replica - ms_interaction) / nt)

    def normal_cdf(value: float) -> float:
        return .5 * (1 + math.erf(value / math.sqrt(2)))

    candidates = []
    for tasks_next in sorted({nt, max(40, nt * 2), max(80, nt * 4), max(160, nt * 8)}):
        for replicas_next in sorted({nr, max(5, nr + 2), max(8, nr + 5)}):
            variance = task_variance / tasks_next + replica_variance / replicas_next + ms_interaction / (tasks_next * replicas_next)
            standard_error = math.sqrt(variance)
            half_width = 1.96 * standard_error
            power = normal_cdf(target_delta / standard_error - 1.96) + normal_cdf(-target_delta / standard_error - 1.96) if standard_error else 1.0
            candidates.append({"tasks_per_family": tasks_next, "package_replicas": replicas_next, "episodes_for_five_conditions": tasks_next * replicas_next * 5,
                               "estimated_ci95_half_width": half_width, "approx_detection_probability": power,
                               "meets_targets": half_width <= target_half_width and power >= .8})
    candidates.sort(key=lambda item: item["episodes_for_five_conditions"])
    recommendation = next((candidate for candidate in candidates if candidate["meets_targets"]), None)
    return {"family": family, "baseline": baseline, "pilot_tasks": nt, "pilot_package_replicas": nr,
            "pilot_mean_quality_delta": grand, "variance_components": {"task": task_variance, "package": replica_variance, "interaction": ms_interaction},
            "target_delta": target_delta, "target_ci95_half_width": target_half_width,
            "candidates": candidates, "smallest_candidate_meeting_targets": recommendation,
            "interpretation": "Exploratory normal-approximation planning from the pilot. Freeze sample size before a new confirmatory campaign; three pilot replicas make package variance uncertain."}
