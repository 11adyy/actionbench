from __future__ import annotations

import random
from statistics import mean


def paired_bootstrap(rows: list[tuple[float, float]], seed: int, samples: int = 10_000) -> dict:
    """Bootstrap paired deltas when each row is independently sampled."""
    if not rows: return {"n": 0, "mean_delta": None, "ci95": None}
    deltas = [left - right for left, right in rows]
    rng = random.Random(seed)
    bootstrap = sorted(mean(deltas[rng.randrange(len(deltas))] for _ in deltas) for _ in range(samples))
    lower = bootstrap[int(.025 * (len(bootstrap) - 1))]; upper = bootstrap[int(.975 * (len(bootstrap) - 1))]
    return {"n": len(deltas), "mean_delta": mean(deltas), "ci95": [lower, upper]}


def crossed_paired_bootstrap(cells: dict[tuple[str, int], tuple[float, float]], seed: int, samples: int = 10_000) -> dict:
    """Resample tasks and generated packages independently in a crossed study.

    A package replica is reused across tasks, while a task is scored by every
    package replica. Resampling only tasks erases package-generation variance;
    treating every cell as independent erases both dependencies.
    """
    task_ids = sorted({task for task, _ in cells})
    replicas = sorted({replica for _, replica in cells})
    if not task_ids or not replicas: return {"n": 0, "n_tasks": 0, "n_replicas": 0, "n_cells": 0, "mean_delta": None, "ci95": None}
    deltas = {key: left - right for key, (left, right) in cells.items()}
    observed = list(deltas.values())
    rng = random.Random(seed)
    bootstrap = []
    for _ in range(samples):
        sampled_tasks = [task_ids[rng.randrange(len(task_ids))] for _ in task_ids]
        sampled_replicas = [replicas[rng.randrange(len(replicas))] for _ in replicas]
        drawn = [deltas[(task, replica)] for task in sampled_tasks for replica in sampled_replicas if (task, replica) in deltas]
        if drawn: bootstrap.append(mean(drawn))
    if not bootstrap:
        return {"n": len(task_ids), "n_tasks": len(task_ids), "n_replicas": len(replicas), "n_cells": len(observed), "mean_delta": mean(observed), "ci95": None}
    bootstrap.sort()
    lower = bootstrap[int(.025 * (len(bootstrap) - 1))]; upper = bootstrap[int(.975 * (len(bootstrap) - 1))]
    return {"n": len(task_ids), "n_tasks": len(task_ids), "n_replicas": len(replicas), "n_cells": len(observed), "mean_delta": mean(observed), "ci95": [lower, upper]}
