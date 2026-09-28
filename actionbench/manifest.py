from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigurationError


@dataclass(frozen=True)
class Task:
    id: str
    family: str
    public_input: Path
    reference_dir: Path
    grader: dict[str, Any]
    split: str
    public_sha256: str | None = None
    reference_sha256: str | None = None


@dataclass(frozen=True)
class Family:
    id: str
    creator_brief: str
    demonstrations: tuple[Path, ...]
    tasks: tuple[Task, ...]


@dataclass(frozen=True)
class Manifest:
    path: Path
    fingerprint: str
    families: tuple[Family, ...]
    dataset_lock: dict[str, Any] | None = None

    @property
    def tasks(self) -> tuple[Task, ...]:
        return tuple(task for family in self.families for task in family.tasks)

    @property
    def development_tasks(self) -> tuple[Task, ...]:
        return tuple(task for task in self.tasks if task.split == "development")

    @property
    def test_tasks(self) -> tuple[Task, ...]:
        return tuple(task for task in self.tasks if task.split == "test")


def load_manifest(path: str | Path, dataset_root: Path) -> Manifest:
    source = Path(path).resolve()
    try:
        raw_text = source.read_text(); raw = json.loads(raw_text)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Invalid manifest {source}: {exc}") from exc
    families: list[Family] = []
    seen: set[str] = set()
    for item in raw.get("families", []):
        family_id = str(item.get("id", ""))
        if not family_id or not item.get("creator_brief"):
            raise ConfigurationError("Each family needs id and creator_brief")
        tasks: list[Task] = []
        for row in item.get("tasks", []):
            task_id = str(row.get("id", ""))
            if not task_id or task_id in seen:
                raise ConfigurationError(f"Task id is missing or duplicated: {task_id!r}")
            seen.add(task_id)
            grader = row.get("grader") or {}
            if not grader.get("image") or not isinstance(grader.get("command"), list):
                raise ConfigurationError(f"Task {task_id} needs a Docker grader image and command")
            split = str(row.get("split", "test"))
            if split not in {"development", "test"}: raise ConfigurationError(f"Task {task_id} has invalid split")
            tasks.append(Task(task_id, family_id, (dataset_root / row["public_input"]).resolve(), (dataset_root / row["reference_dir"]).resolve(), grader, split, row.get("public_sha256"), row.get("reference_sha256")))
        if not tasks:
            raise ConfigurationError(f"Family {family_id} has no tasks")
        demos = tuple((dataset_root / d).resolve() for d in item.get("demonstrations", []))
        families.append(Family(family_id, str(item["creator_brief"]), demos, tuple(tasks)))
    if not families or not any(task.split == "test" for family in families for task in family.tasks):
        raise ConfigurationError("Manifest has no families")
    return Manifest(source, hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest(), tuple(families), raw.get("dataset_lock"))


def verify_data(manifest: Manifest) -> list[str]:
    failures = []
    for task in manifest.tasks:
        if not task.public_input.is_file(): failures.append(f"Missing public input: {task.public_input}")
        elif task.public_sha256 and hashlib.sha256(task.public_input.read_bytes()).hexdigest() != task.public_sha256:
            failures.append(f"Public input changed: {task.public_input}")
        if not task.reference_dir.is_dir(): failures.append(f"Missing reference directory: {task.reference_dir}")
        else:
            gold = task.reference_dir / "gold.json"
            if task.reference_sha256 and (not gold.is_file() or hashlib.sha256(gold.read_bytes()).hexdigest() != task.reference_sha256):
                failures.append(f"Reference changed: {gold}")
        if manifest.dataset_lock and (not task.public_sha256 or not task.reference_sha256):
            failures.append(f"Study task lacks content hashes: {task.id}")
    if manifest.dataset_lock:
        raw = manifest.tasks[0].public_input.parents[2] / "raw"
        filenames = {"mbpp_plus_v0.1.0": "MbppPlus-v0.1.0.jsonl.gz", "hotpot_dev_distractor": "hotpot_dev_distractor_v1.json"}
        for key, filename in filenames.items():
            entry = manifest.dataset_lock.get(key)
            path = raw / filename
            if not isinstance(entry, dict) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
                failures.append(f"Dataset source changed or missing: {path}")
    for family in manifest.families:
        for demo in family.demonstrations:
            if not demo.exists(): failures.append(f"Missing demonstration: {demo}")
    return failures
