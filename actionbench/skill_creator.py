from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from .broker import Broker
from .errors import ActionBenchError
from .manifest import Family


BASE = """You create one reusable agent skill for a benchmark family. Return only valid JSON. The skill must give operational instructions that are useful on unseen instances, never answers to test examples. Do not use network, credentials, shell commands, subprocesses, or external packages."""
SKILL_PROMPT = BASE + """ Return exactly {\"skill_md\": string}. The agent always has a sandboxed Python code tool but no direct model-call tool."""
ACTION_PROMPT = BASE + """ Return exactly {\"skill_md\": string, \"actions\": array}. Each action has id, code, command. `code` is complete Python using `from action_sdk import ActionContext`; it reads {kind,input} JSON from stdin, creates ActionContext(input['input']), may call ctx.call_llm, then calls ctx.emit(object). `command` must be [\"python\", \"/action/main.py\"]. Actions must be narrow reusable procedures, not an entire agent loop."""


def _decode(text: str, kind: str) -> dict:
    try: value = json.loads(text)
    except json.JSONDecodeError as exc: raise ActionBenchError(f"Creator returned invalid JSON: {exc}") from exc
    if not isinstance(value.get("skill_md"), str): raise ActionBenchError("Creator output lacks skill_md")
    if kind == "action" and not isinstance(value.get("actions"), list): raise ActionBenchError("Action package lacks actions")
    if kind == "skill" and set(value) != {"skill_md"}: raise ActionBenchError("Conventional skill package must contain only skill_md")
    return value


def _validate_action(action: dict) -> None:
    action_id, code, command = action.get("id"), action.get("code"), action.get("command")
    if not isinstance(action_id, str) or not action_id.replace("-", "").isalnum(): raise ActionBenchError("Invalid action id")
    if command != ["python", "/action/main.py"]: raise ActionBenchError(f"Action {action_id} must use the approved command")
    if not isinstance(code, str) or "from action_sdk import ActionContext" not in code or "ctx.emit" not in code: raise ActionBenchError(f"Action {action_id} violates the protocol")
    try: ast.parse(code)
    except SyntaxError as exc: raise ActionBenchError(f"Action {action_id} has invalid Python: {exc}") from exc


def create_package(broker: Broker, episode_id: str, family: Family, replica: int, kind: str, destination: Path, feedback: list[dict] | None = None) -> str:
    demos = [{"path": str(path), "content": path.read_text() if path.is_file() else "directory"} for path in family.demonstrations]
    prompt = json.dumps({"family": family.id, "brief": family.creator_brief, "demonstrations": demos, "replica": replica, "previous_development_feedback": feedback or []}, sort_keys=True)
    result = broker.call(episode_id, f"package-{kind}-draft", ACTION_PROMPT if kind == "action" else SKILL_PROMPT, prompt, 4096)
    package = _decode(result.text, kind)
    if kind == "action":
        seen = set()
        for action in package["actions"]:
            _validate_action(action)
            if action["id"] in seen: raise ActionBenchError("Action ids must be unique")
            seen.add(action["id"])
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "SKILL.md").write_text(package["skill_md"])
    if kind == "action":
        actions = destination / "actions"; actions.mkdir()
        from . import action_sdk
        sdk = Path(action_sdk.__file__).read_text()
        for action in package["actions"]:
            root = actions / action["id"]; root.mkdir()
            (root / "main.py").write_text(action["code"])
            (root / "action_sdk.py").write_text(sdk)
            (root / "action.json").write_text(json.dumps({"id": action["id"], "command": action["command"]}, sort_keys=True))
    digest = hashlib.sha256()
    for file in sorted(destination.rglob("*")):
        if file.is_file(): digest.update(file.relative_to(destination).as_posix().encode()); digest.update(file.read_bytes())
    (destination / "package.json").write_text(json.dumps({"family": family.id, "replica": replica, "kind": kind, "hash": digest.hexdigest()}, indent=2))
    return digest.hexdigest()
