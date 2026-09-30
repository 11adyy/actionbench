from __future__ import annotations

import json
import hashlib
import math
from collections import defaultdict

from .statistics import crossed_paired_bootstrap


def build_report(config, store) -> dict:
    rows = store.conn.execute("""SELECT e.task_id,e.family,e.condition,e.replica,e.status,e.retryable,e.error,e.failure_kind,e.duration_seconds,ev.score_json,
                              COALESCE(SUM(CASE WHEN r.state IN ('rejected','policy_rejected') THEN 0 ELSE COALESCE(r.actual_usd,r.reserved_usd) END),0) cost,
                              SUM(CASE WHEN r.state='completed' THEN 1 ELSE 0 END) model_calls,
                              SUM(CASE WHEN r.state IN ('reserved','submitted','unknown_outcome') THEN 1 ELSE 0 END) uncertain_requests
                              FROM episodes e LEFT JOIN evaluations ev ON ev.episode_id=e.episode_id
                              LEFT JOIN requests r ON r.episode_id=e.episode_id WHERE e.campaign=? GROUP BY e.episode_id""", (config.campaign,)).fetchall()
    groups = defaultdict(lambda: {"planned": 0, "completed": 0, "execution_failed": 0, "pending": 0, "blocked": 0, "scored": [], "costs": [], "terminal_costs": [], "terminal_seconds": [], "terminal_calls": [], "uncertain_requests": 0, "failure_kinds": defaultdict(int)})
    creation = defaultdict(lambda: {"episodes": 0, "completed": 0, "failed": 0, "total_usd": 0.0})
    creation_costs = defaultdict(float)
    phase_costs = defaultdict(float)
    phase_calls = defaultdict(int)
    for row in rows:
        phase = "controls" if row["family"] == "integration" else ("development" if row["task_id"].startswith(("creation-dev:", "creation-probe:")) else ("package_creation" if row["task_id"].startswith("creation:") else "test"))
        phase_costs[phase] += float(row["cost"])
        phase_calls[phase] += int(row["model_calls"] or 0)
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
        elif row["status"] == "failed" and not row["retryable"]:
            group["execution_failed"] += 1
            group["failure_kinds"][row["failure_kind"] or "unclassified_legacy"] += 1
        elif row["status"] == "blocked": group["blocked"] += 1
        else: group["pending"] += 1
        group.setdefault("terminal", 0); group["terminal"] += terminal
    binding = store.study_binding(config.campaign)
    terminal_total = sum(group["terminal"] for group in groups.values())
    complete = bool(binding and terminal_total == binding["planned_test_episodes"] and sum(group["blocked"] for group in groups.values()) == 0)
    creation_rows = store.conn.execute("""SELECT condition,status,COUNT(*) n FROM episodes WHERE campaign=? AND task_id LIKE 'creation:%' GROUP BY condition,status""", (config.campaign,)).fetchall()
    package_creation = defaultdict(lambda: {"attempted": 0, "completed": 0, "failed": 0, "pending": 0})
    for row in creation_rows:
        item = package_creation[row["condition"]]
        item["attempted"] += row["n"]
        item[row["status"] if row["status"] in {"completed", "failed"} else "pending"] += row["n"]
    for item in package_creation.values():
        item["failure_rate"] = item["failed"] / item["attempted"] if item["attempted"] else None
    output = {"campaign": config.campaign, "analysis_status": "complete" if complete else "provisional", "planned_test_episodes": binding["planned_test_episodes"] if binding else None, "groups": {}, "package_creation": dict(package_creation), "skill_creation": dict(creation), "status": store.campaign_status(config.campaign)}
    policy_events = defaultdict(int)
    policy_test_by_family = defaultdict(int)
    for row in store.conn.execute("""SELECT e.family,e.task_id,r.state,r.incomplete_reason FROM requests r
                                   JOIN episodes e ON e.episode_id=r.episode_id WHERE e.campaign=?
                                   AND (r.state='policy_rejected' OR r.incomplete_reason='content_filter')""", (config.campaign,)):
        phase = "controls" if row["family"] == "integration" else ("development" if row["task_id"].startswith(("creation-dev:", "creation-probe:")) else ("package_creation" if row["task_id"].startswith("creation:") else "test"))
        policy_events[phase] += 1
        if phase == "test": policy_test_by_family[row["family"]] += 1
    output["provider_policy_events_by_phase"] = dict(policy_events)
    technical_failure_kinds = {"unclassified_legacy", "unclassified_agent_error", "harness_error", "grader_error", "infrastructure_error"}
    technical_by_family = {family: sum(count for name, group in groups.items() if name.startswith(f"{family}:")
                                       for kind, count in group["failure_kinds"].items() if kind in technical_failure_kinds)
                           for family in {row["family"] for row in rows if row["family"] != "integration"}}
    action_invocations_by_family = {row["family"]: row["invocations"] for row in store.conn.execute("""SELECT e.family,COUNT(*) invocations
        FROM action_runs ar JOIN episodes e ON e.episode_id=ar.episode_id
        WHERE e.campaign=? AND e.condition='action' AND e.family!='integration' AND e.task_id NOT LIKE 'creation%'
        GROUP BY e.family""", (config.campaign,))}
    output["action_invocations_by_family"] = action_invocations_by_family
    def preparation_cost(family: str, condition: str, replica: int) -> float:
        if condition == "plain": return 0.0
        paired_skill = creation_costs[(family, "skill", replica)]
        return paired_skill if condition in {"skill", "improvised"} else paired_skill + creation_costs[(family, condition, replica)]

    output["policy_preparation_usd_by_replica"] = {
        f"{family}:{condition}:{replica}": preparation_cost(family, condition, replica)
        for family, condition, replica in sorted({(row["family"], row["condition"], row["replica"])
                                                  for row in rows if row["family"] != "integration" and not row["task_id"].startswith("creation")})
    }
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
            "failure_kinds": dict(group["failure_kinds"]),
        }
    by_cell = {}
    for row in rows:
        if row["task_id"].startswith("creation") or row["family"] == "integration": continue
        terminal = (row["status"] == "completed" and row["score_json"] is not None) or (row["status"] == "failed" and not row["retryable"])
        score = 0.0
        if row["status"] == "completed" and row["score_json"]: score = float(json.loads(row["score_json"])["primary"])
        by_cell[(row["family"], row["task_id"], row["replica"], row["condition"])] = {"terminal": terminal, "score": score, "cost": float(row["cost"])}
    available_packages = {(r["family"], r["replica"], r["condition"]) for r in store.conn.execute(
        "SELECT family,replica,condition FROM generated_packages WHERE campaign=?", (config.campaign,)).fetchall()}
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
            uses = config.analysis.amortization_uses
            total_cost_cells = {(task, replica): (
                action[1] + preparation_cost(family, "action", replica) / uses,
                other[1] + preparation_cost(family, baseline, replica) / uses)
                for task, pairs in tasks.items() for replica, (action, other) in pairs}
            mean_runtime_delta = sum(action[1] - other[1] for pairs in tasks.values() for _, (action, other) in pairs) / sum(len(pairs) for pairs in tasks.values())
            per_replica_creation = [preparation_cost(family, "action", replica) - preparation_cost(family, baseline, replica) for replica in replicas[family]]
            mean_creation_delta = sum(per_replica_creation) / len(per_replica_creation) if per_replica_creation else 0.0
            break_even = None if mean_runtime_delta >= 0 else max(0, int(math.ceil(mean_creation_delta / -mean_runtime_delta)))
            quality = crossed_paired_bootstrap(quality_cells, salt)
            usd = crossed_paired_bootstrap(cost_cells, salt + 1)
            total_usd = crossed_paired_bootstrap(total_cost_cells, salt + 2)
            action_available = all((family, replica, "action") in available_packages for replica in replicas[family])
            baseline_available = baseline != "skill_script" or all((family, replica, "skill_script") in available_packages for replica in replicas[family])
            interpretable = action_available and baseline_available
            inferential = (complete and interpretable and quality["n_tasks"] >= 10 and quality["n_replicas"] >= 3
                          and not excluded[family] and not technical_by_family.get(family) and not policy_test_by_family[family]
                          and action_invocations_by_family.get(family, 0) > 0)
            if not inferential:
                quality["ci95"] = None; usd["ci95"] = None; total_usd["ci95"] = None
            reason = None if interpretable else ("action_package_unavailable" if not action_available else "baseline_package_unavailable")
            margin = config.analysis.quality_noninferiority_margin
            supports_noninferiority = bool(inferential and quality["ci95"][0] > -margin)
            supports_runtime_saving = bool(inferential and usd["ci95"][1] < 0)
            supports_total_saving = bool(inferential and total_usd["ci95"][1] < 0)
            comparisons[f"{family}:action_minus_{baseline}"] = {
                "quality": quality,
                "usd": usd,
                "total_usd_at_declared_uses": total_usd if interpretable else None,
                "inferential_interval_available": inferential,
                "interpretable": interpretable,
                "invalid_reason": reason,
                "incomplete_pairs_excluded": excluded[family],
                "supports_total_cost_saving_with_quality_noninferiority": (supports_noninferiority and supports_total_saving) if inferential else None,
                "noninferiority_margin": margin,
                "supports_quality_noninferiority": supports_noninferiority if inferential else None,
                "supports_runtime_cost_saving": supports_runtime_saving if inferential else None,
                "supports_total_cost_saving": supports_total_saving if inferential else None,
                "amortization": {"mean_creation_delta_usd_per_replica": mean_creation_delta, "mean_runtime_delta_usd_per_episode": mean_runtime_delta, "break_even_uses_per_package": break_even,
                                 "declared_uses": config.analysis.amortization_uses,
                                 "mean_total_delta_at_declared_uses": mean_creation_delta + config.analysis.amortization_uses * mean_runtime_delta} if interpretable else None,
            }
    output["paired_comparisons"] = comparisons
    usage = store.conn.execute("""SELECT e.family,e.condition,ar.action_id,COUNT(*) invocations,
                                  SUM(CASE WHEN ar.state='completed' THEN 1 ELSE 0 END) completed
                                  FROM action_runs ar JOIN episodes e ON e.episode_id=ar.episode_id
                                  WHERE e.campaign=? AND e.family!='integration' AND e.task_id NOT LIKE 'creation%'
                                  GROUP BY e.family,e.condition,ar.action_id""", (config.campaign,)).fetchall()
    output["procedure_usage"] = [dict(row) for row in usage]
    output["model_cost_by_phase_usd"] = dict(phase_costs)
    output["model_calls_by_phase"] = dict(phase_calls)
    package_failures = sum(item["failed"] for kind, item in package_creation.items() if kind in {"action", "skill_script"})
    missing_action = package_creation.get("action", {}).get("completed", 0) == 0
    technical_failures = sum(technical_by_family.values())
    provider_rejections = sum(group["failure_kinds"].get("provider_rejected", 0) for group in groups.values())
    scored_action = sum(group["completed"] for name, group in groups.items() if name.endswith(":action"))
    reasons = []
    if missing_action: reasons.append("no_action_package_created")
    if package_failures: reasons.append("procedure_package_creation_failed")
    if not scored_action: reasons.append("no_scored_action_episode")
    if technical_failures: reasons.append("technical_or_unclassified_episode_failures")
    if provider_rejections: reasons.append("provider_policy_rejections_observed")
    if policy_events.get("test", 0): reasons.append("provider_content_filter_or_policy_rejection_in_test")
    if complete:
        for family in sorted({row["family"] for row in rows if row["family"] != "integration" and not row["task_id"].startswith("creation")}):
            if not action_invocations_by_family.get(family, 0): reasons.append(f"no_action_invocation_observed:{family}")
    output["execution_status"] = "terminal" if complete else ("blocked" if any(group["blocked"] for group in groups.values()) else "paused")
    output["validation_status"] = "failed" if missing_action or technical_failures else ("inconclusive" if reasons or not complete else "passed")
    output["validation_reasons"] = reasons
    output["scientific_status"] = "diagnostic_only" if output["validation_status"] != "passed" else ("confirmatory" if config.analysis.study_role == "confirmatory" else "exploratory")
    output["analysis_status"] = output["scientific_status"]
    output["pricing_configured"] = all(value > 0 for value in (config.provider.input_usd_per_million, config.provider.output_usd_per_million))
    return output
