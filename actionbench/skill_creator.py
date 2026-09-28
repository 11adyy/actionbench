from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .broker import Broker
from .errors import ActionBenchError
from .manifest import Family


CREATOR_INSTRUCTIONS = """You create experimental agent skills. Return only JSON with keys skill_md and actions. skill_md is ordinary markdown instructions. actions is an array. Each action has id, code, command. code is a complete Python executable that reads one JSON input line from stdin, creates ActionContext from `from action_sdk import ActionContext` using input['input'], may call ctx.call_llm(), and ends with ctx.emit(a JSON object). command is an array used inside python:3.11-slim. Do not use network, environment credentials, shell, subprocess, or external packages. Make the ordinary instructions and action procedure semantically equivalent. Actions should be narrow, reusable subprocedures, not entire agent loops."""


def _json_object(text: str) -> dict:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ActionBenchError(f"Skill creator did not produce valid JSON: {exc}") from exc
    if not isinstance(obj.get("skill_md"), str) or not isinstance(obj.get("actions"), list):
        raise ActionBenchError("Skill creator JSON lacks skill_md or actions")
    return obj


def _validate_action(action: dict) -> None:
    action_id = action.get("id")
    code = action.get("code")
    command = action.get("command")
    if not isinstance(action_id, str) or not action_id.replace("-", "").isalnum():
        raise ActionBenchError("Action id must be alphanumeric with hyphens")
    if not isinstance(code, str) or "from action_sdk import ActionContext" not in code or "ctx.emit" not in code:
        raise ActionBenchError(f"Action {action_id!r} does not use the action protocol")
    if not isinstance(command, list) or not all(isinstance(v, str) for v in command):
        raise ActionBenchError(f"Action {action_id!r} has invalid command")


def create_skill(broker: Broker, episode_id: str, family: Family, replica: int, destination: Path) -> str:
    demos = []
    for demo in family.demonstrations:
        demos.append({"path": str(demo), "content": demo.read_text() if demo.is_file() else "directory provided"})
    prompt = json.dumps({"family": family.id, "brief": family.creator_brief, "demonstrations": demos, "replica": replica}, sort_keys=True)
    result = broker.call(episode_id, f"create-skill-{family.id}-{replica}", CREATOR_INSTRUCTIONS, prompt, 4096)
    generated = _json_object(result.text)
    action_ids = set()
    for action in generated["actions"]:
        _validate_action(action)
        if action["id"] in action_ids: raise ActionBenchError("Action ids must be unique")
        action_ids.add(action["id"])
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "SKILL.md").write_text(generated["skill_md"])
    actions_root = destination / "actions"; actions_root.mkdir()
    for action in generated["actions"]:
        root = actions_root / action["id"]; root.mkdir()
        (root / "main.py").write_text(action["code"])
        from . import action_sdk
        (root / "action_sdk.py").write_text(Path(action_sdk.__file__).read_text())
        (root / "action.json").write_text(json.dumps({"id": action["id"], "command": action["command"]}, sort_keys=True))
    digest = hashlib.sha256()
    for file in sorted(destination.rglob("*")):
        if file.is_file(): digest.update(file.relative_to(destination).as_posix().encode()); digest.update(file.read_bytes())
    (destination / "package.json").write_text(json.dumps({"family": family.id, "replica": replica, "hash": digest.hexdigest()}, indent=2))
    return digest.hexdigest()
