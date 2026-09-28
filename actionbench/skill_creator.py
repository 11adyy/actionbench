from __future__ import annotations

import ast
import hashlib
import json
import os
import uuid
from pathlib import Path

from .broker import Broker
from .errors import ActionBenchError
from .manifest import Family


BASE = """You create one reusable agent skill for a benchmark family. Return only valid JSON. The skill must give operational instructions that are useful on unseen instances, never answers to test examples. Do not use network, credentials, shell commands, subprocesses, or external packages."""
SKILL_PROMPT = BASE + """ Return exactly {\"skill_md\": string}. The agent always has a sandboxed Python code tool but no direct model-call tool."""
SCRIPT_PROMPT = BASE + """ Return exactly {\"skill_md\": string, \"procedures\": array}. `skill_md` must reproduce the supplied base skill byte-for-byte. Each procedure has id, description, input_schema, code, command. `description` and `input_schema` tell the agent how to call it. `code` is complete Python: it reads {kind,input} JSON from stdin and emits {kind,result,output:object}. It must be deterministic and may not import action_sdk or request an LLM. `command` must be [\"python\", \"/action/main.py\"]. Procedures must be narrow reusable procedures, not an entire agent loop."""
ACTION_PROMPT = BASE + """ Return exactly {\"skill_md\": string, \"procedures\": array}. `skill_md` must reproduce the supplied base skill byte-for-byte. Each procedure has id, description, input_schema, code, command. `description` and `input_schema` tell the agent how to call it. `code` is complete Python using `from action_sdk import ActionContext`; it reads {kind,input} JSON from stdin, creates ActionContext(input['input']), may call ctx.call_llm, then calls ctx.emit(object). `command` must be [\"python\", \"/action/main.py\"]. Procedures must be narrow reusable procedures, not an entire agent loop."""


def _decode(text: str, kind: str) -> dict:
    try: value = json.loads(text)
    except json.JSONDecodeError as exc: raise ActionBenchError(f"Creator returned invalid JSON: {exc}") from exc
    if not isinstance(value.get("skill_md"), str): raise ActionBenchError("Creator output lacks skill_md")
    if kind in {"skill_script", "action"} and not isinstance(value.get("procedures"), list): raise ActionBenchError("Procedure package lacks procedures")
    if kind == "skill" and set(value) != {"skill_md"}: raise ActionBenchError("Conventional skill package must contain only skill_md")
    return value


def _validate_procedure(procedure: dict, kind: str) -> None:
    procedure_id, code, command = procedure.get("id"), procedure.get("code"), procedure.get("command")
    if not isinstance(procedure_id, str) or not procedure_id.replace("-", "").isalnum(): raise ActionBenchError("Invalid procedure id")
    if not isinstance(procedure.get("description"), str) or not procedure["description"].strip(): raise ActionBenchError(f"Procedure {procedure_id} needs a description")
    if not isinstance(procedure.get("input_schema"), dict): raise ActionBenchError(f"Procedure {procedure_id} needs an input_schema object")
    if command != ["python", "/action/main.py"]: raise ActionBenchError(f"Procedure {procedure_id} must use the approved command")
    if not isinstance(code, str): raise ActionBenchError(f"Procedure {procedure_id} needs Python code")
    if kind == "action" and "ctx.emit" not in code: raise ActionBenchError(f"Procedure {procedure_id} violates the action protocol")
    if kind == "action" and "from action_sdk import ActionContext" not in code: raise ActionBenchError(f"Action {procedure_id} cannot call the controlled LLM")
    if kind == "skill_script" and ("action_sdk" in code or "llm_request" in code or "call_llm" in code): raise ActionBenchError(f"Script {procedure_id} attempts to access the LLM")
    try: ast.parse(code)
    except SyntaxError as exc: raise ActionBenchError(f"Procedure {procedure_id} has invalid Python: {exc}") from exc


def _package_hash(destination: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(destination.rglob("*")):
        if file.is_file() and file.name != "package.json":
            digest.update(file.relative_to(destination).as_posix().encode()); digest.update(file.read_bytes())
    return digest.hexdigest()


def create_package(broker: Broker, episode_id: str, family: Family, replica: int, kind: str, destination: Path, feedback: list[dict] | None = None, base_skill_md: str | None = None) -> str:
    demos = [{"path": str(path), "content": path.read_text() if path.is_file() else "directory"} for path in family.demonstrations]
    prompt = json.dumps({"family": family.id, "brief": family.creator_brief, "demonstrations": demos, "replica": replica, "base_skill_md": base_skill_md, "previous_development_feedback": feedback or []}, sort_keys=True)
    if destination.exists():
        saved = destination / "package.json"
        if saved.is_file():
            metadata = json.loads(saved.read_text())
            if metadata.get("family") == family.id and metadata.get("replica") == replica and metadata.get("kind") == kind and metadata.get("hash") == _package_hash(destination):
                return metadata["hash"]
        raise ActionBenchError(f"Incomplete package directory needs manual review: {destination}")
    instructions = {"skill": SKILL_PROMPT, "skill_script": SCRIPT_PROMPT, "action": ACTION_PROMPT}[kind]
    result = broker.call(episode_id, f"package-{kind}-draft", instructions, prompt, 4096)
    package = _decode(result.text, kind)
    if kind in {"skill_script", "action"}:
        if package["skill_md"] != base_skill_md: raise ActionBenchError("Procedure package changed its paired conventional skill")
        if not package["procedures"]: raise ActionBenchError("Procedure package must expose at least one procedure")
        seen = set()
        for procedure in package["procedures"]:
            _validate_procedure(procedure, kind)
            if procedure["id"] in seen: raise ActionBenchError("Procedure ids must be unique")
            seen.add(procedure["id"])
    staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.building")
    staging.mkdir(parents=True, exist_ok=False)
    (staging / "SKILL.md").write_text(package["skill_md"])
    if kind in {"skill_script", "action"}:
        procedures = staging / "procedures"; procedures.mkdir()
        from . import action_sdk
        sdk = Path(action_sdk.__file__).read_text()
        for procedure in package["procedures"]:
            root = procedures / procedure["id"]; root.mkdir()
            (root / "main.py").write_text(procedure["code"])
            if kind == "action": (root / "action_sdk.py").write_text(sdk)
            (root / "procedure.json").write_text(json.dumps({key: procedure[key] for key in ("id", "description", "input_schema", "command")}, sort_keys=True))
    digest = _package_hash(staging)
    (staging / "package.json").write_text(json.dumps({"family": family.id, "replica": replica, "kind": kind, "hash": digest}, indent=2))
    os.replace(staging, destination)
    return digest
