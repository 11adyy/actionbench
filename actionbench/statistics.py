from __future__ import annotations

import random
from statistics import mean


def paired_bootstrap(rows: list[tuple[float, float]], seed: int, samples: int = 10_000) -> dict:
    """Paired percentile bootstrap for action minus baseline, deterministic per campaign."""
    if not rows: return {"n": 0, "mean_delta": None, "ci95": None}
    deltas = [left - right for left, right in rows]
    rng = random.Random(seed)
    bootstrap = sorted(mean(deltas[rng.randrange(len(deltas))] for _ in deltas) for _ in range(samples))
    lower = bootstrap[int(.025 * (samples - 1))]; upper = bootstrap[int(.975 * (samples - 1))]
    return {"n": len(deltas), "mean_delta": mean(deltas), "ci95": [lower, upper]}
