from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError


@dataclass(frozen=True)
class ProviderConfig:
    kind: str
    base_url: str
    api_key_env: str
    model: str
    input_usd_per_million: float
    cached_input_usd_per_million: float
    output_usd_per_million: float
    cache_write_usd_per_million: float | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class BudgetConfig:
    usd: float
    max_input_tokens: int
    max_output_tokens: int
    max_llm_calls: int


@dataclass(frozen=True)
class ExecutionConfig:
    docker_image: str
    timeout_seconds: int
    memory_mb: int
    cpus: int


@dataclass(frozen=True)
class Config:
    campaign: str
    provider: ProviderConfig
    budget: BudgetConfig
    execution: ExecutionConfig
    dataset_root: Path
    artifact_root: Path
    replicas: int
    conditions: tuple[str, ...]
    source_path: Path
    fingerprint: str

    @property
    def db_path(self) -> Path:
        # v3 adds a deterministic-script baseline and a new package ledger.  A
        # separate ledger prevents an in-progress earlier design from being
        # interpreted as a result from this different experiment.
        return self.artifact_root / "actionbench-v3.sqlite3"


def _required(mapping: dict, key: str):
    if key not in mapping:
        raise ConfigurationError(f"Missing configuration field: {key}")
    return mapping[key]


def load_config(path: str | Path) -> Config:
    source = Path(path).resolve()
    try:
        raw_text = source.read_text()
        raw = json.loads(raw_text)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Configuration does not exist: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"Configuration must be valid JSON: {exc}") from exc
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    provider = _required(raw, "provider")
    budget = _required(raw, "budget")
    execution = _required(raw, "execution")
    root = source.parent
    cfg = Config(
        campaign=str(_required(raw, "campaign")),
        provider=ProviderConfig(**provider),
        budget=BudgetConfig(**budget),
        execution=ExecutionConfig(**execution),
        dataset_root=(root / str(_required(raw, "dataset_root"))).resolve(),
        artifact_root=(root / str(_required(raw, "artifact_root"))).resolve(),
        replicas=int(_required(raw, "replicas")),
        conditions=tuple(_required(raw, "conditions")),
        source_path=source,
        fingerprint=hashlib.sha256(canonical.encode()).hexdigest(),
    )
    if not cfg.campaign or cfg.replicas < 1 or cfg.budget.usd <= 0:
        raise ConfigurationError("campaign, replicas, and budget.usd must be positive")
    if set(cfg.conditions) != {"plain", "skill", "skill_script", "improvised", "action"}:
        raise ConfigurationError("conditions must contain plain, skill, skill_script, improvised, and action exactly")
    return cfg


def api_key(config: Config) -> str:
    value = os.environ.get(config.provider.api_key_env)
    if not value:
        raise ConfigurationError(f"Set {config.provider.api_key_env} before making real API requests")
    return value
