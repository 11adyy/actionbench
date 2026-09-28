from __future__ import annotations

import random
from statistics import mean


def paired_bootstrap(rows: list[tuple[float, float]], seed: int, samples: int = 10_000) -> dict:
    """Bootstrap paired deltas when each row is independently sampled."""
    if not rows: return {"n": 0, "mean_delta": None, "ci95": None}
    deltas = [left - right for left, right in rows]
    rng = random.Random(seed)
    bootstrap = sorted(mean(deltas[rng.randrange(len(deltas))] for _ in deltas) for _ in range(samples))
    lower = bootstrap[int(.025 * (samples - 1))]; upper = bootstrap[int(.975 * (samples - 1))]
    return {"n": len(deltas), "mean_delta": mean(deltas), "ci95": [lower, upper]}


def clustered_paired_bootstrap(clusters: dict[str, list[tuple[float, float]]], seed: int, samples: int = 10_000) -> dict:
    """Resample tasks, averaging replica-level pairs within each task first.

    Packages vary by replica, but the benchmark task is shared across replicas.
    Treating all task/replica cells as independent would make the interval too narrow.
    """
    task_deltas = [mean(left - right for left, right in cells) for cells in clusters.values() if cells]
    n_cells = sum(len(cells) for cells in clusters.values())
    if not task_deltas: return {"n": 0, "n_tasks": 0, "n_cells": 0, "mean_delta": None, "ci95": None}
    rng = random.Random(seed)
    bootstrap = sorted(mean(task_deltas[rng.randrange(len(task_deltas))] for _ in task_deltas) for _ in range(samples))
    lower = bootstrap[int(.025 * (samples - 1))]; upper = bootstrap[int(.975 * (samples - 1))]
    return {"n": len(task_deltas), "n_tasks": len(task_deltas), "n_cells": n_cells, "mean_delta": mean(task_deltas), "ci95": [lower, upper]}
