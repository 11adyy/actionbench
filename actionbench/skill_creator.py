from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .broker import Broker
from .contracts import package_format
from .errors import ActionBenchError, BudgetExceeded
from .manifest import Family


BASE = """Create reusable instructions or narrow procedures for a benchmark family. Use only development examples; never include their answers or hidden test answers. Do not use network, credentials, shell commands, subprocesses, or external packages. Procedure IDs must match ^[A-Za-z][A-Za-z0-9_-]{0,63}$."""
SKILL_PROMPT = BASE + """ Return skill_md as useful operational instructions. The agent has a sandboxed Python code tool but no direct model-call tool."""
SCRIPT_PROMPT = BASE + """ Return one narrow deterministic procedure; the paired skill is supplied by the harness. Each procedure has id, description, input_schema_json (a JSON Schema object serialized as a string), and complete Python code. Keep the code concise and focused on one useful subtask. The harness sends one JSON line shaped as {\"kind\":\"input\",\"input\":object}; read exactly that line and take the procedure arguments from message['input'], not from the top level. Emit one JSON line shaped as {\"kind\":\"result\",\"output\":object} and exit. It may not import action_sdk or request an LLM. Do not write an entire agent loop."""
ACTION_PROMPT = BASE + """ Return one narrow reusable procedure; the paired skill is supplied by the harness. Each procedure has id, description, input_schema_json (a JSON Schema object serialized as a string), and complete Python code. Keep the code concise and focused on one useful subtask. Use `from action_sdk import ActionContext`; read one JSON input line; construct ctx = ActionContext(message['input']); make at least one controlled ctx.call_llm(prompt, instructions='', max_output_tokens=1024) on a normal valid input; finish with ctx.emit(object). The call must help perform the procedure. Do not write an entire agent loop."""

PROCEDURE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
DEVELOPMENT_PROMPT_BYTES = 10_000


def _development_prompt_examples(family: Family) -> list[dict]:
    """Choose complete public examples once, identically for every package kind.

    Long QA contexts previously consumed most of the creator's episode token
    budget on every revision. Shortest-first selection is deterministic and
    does not inspect references, grades, or hidden test tasks.
    """
    candidates = [(task.id, task.public_input.read_text()) for task in getattr(family, "tasks", ()) if task.split == "development"]
    if not candidates:
        return []
    candidates.sort(key=lambda item: (len(item[1].encode()), item[0]))
    selected: list[dict] = []
    total = 0
    for task_id, content in candidates:
        size = len(content.encode())
        if total + size <= DEVELOPMENT_PROMPT_BYTES:
            selected.append({"task_id": task_id, "public_input": content})
            total += size
    if not selected:
        raise ActionBenchError(f"No complete development example for {family.id} fits the creator context budget")
    return selected


def _decode(text: str, kind: str) -> dict:
    try: value = json.loads(text)
    except json.JSONDecodeError as exc: raise ActionBenchError(f"Creator returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict): raise ActionBenchError("Creator output must be a JSON object")
    if kind == "skill":
        if set(value) != {"skill_md"} or not isinstance(value["skill_md"], str) or not value["skill_md"].strip():
            raise ActionBenchError("Conventional skill package must contain only nonempty skill_md")
    elif set(value) != {"procedures"} or not isinstance(value["procedures"], list):
        raise ActionBenchError("Procedure package must contain only procedures")
    return value


def _validate_procedure(procedure: dict, kind: str) -> dict:
    if not isinstance(procedure, dict): raise ActionBenchError("Each procedure must be a JSON object")
    procedure_id, code = procedure.get("id"), procedure.get("code")
    if not isinstance(procedure_id, str) or not PROCEDURE_ID.fullmatch(procedure_id): raise ActionBenchError("Invalid procedure id")
    if not isinstance(procedure.get("description"), str) or not procedure["description"].strip(): raise ActionBenchError(f"Procedure {procedure_id} needs a description")
    try: schema = json.loads(procedure["input_schema_json"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc: raise ActionBenchError(f"Procedure {procedure_id} needs valid input_schema_json") from exc
    if not isinstance(schema, dict) or schema.get("type") != "object": raise ActionBenchError(f"Procedure {procedure_id} needs an object input_schema")
    try: Draft202012Validator.check_schema(schema)
    except SchemaError as exc: raise ActionBenchError(f"Procedure {procedure_id} has invalid JSON Schema: {exc.message}") from exc
    if not isinstance(code, str): raise ActionBenchError(f"Procedure {procedure_id} needs Python code")
    if kind == "action" and "ctx.emit" not in code: raise ActionBenchError(f"Procedure {procedure_id} violates the action protocol")
    if kind == "action" and "from action_sdk import ActionContext" not in code: raise ActionBenchError(f"Action {procedure_id} cannot call the controlled LLM")
    if kind == "skill_script" and ("action_sdk" in code or "llm_request" in code or "call_llm" in code): raise ActionBenchError(f"Script {procedure_id} attempts to access the LLM")
    try: tree = ast.parse(code)
    except SyntaxError as exc: raise ActionBenchError(f"Procedure {procedure_id} has invalid Python: {exc}") from exc
    banned_imports = {"subprocess", "socket", "requests", "urllib", "http", "ftplib", "smtplib", "ctypes"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(alias.name.split(".")[0] in banned_imports for alias in node.names):
            raise ActionBenchError(f"Procedure {procedure_id} imports a disallowed module")
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in banned_imports:
            raise ActionBenchError(f"Procedure {procedure_id} imports a disallowed module")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec", "compile", "__import__"}:
            raise ActionBenchError(f"Procedure {procedure_id} uses a disallowed dynamic execution call")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "os" and node.func.attr in {"system", "popen"}:
            raise ActionBenchError(f"Procedure {procedure_id} uses a disallowed shell call")
    if kind == "action" and not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "call_llm" for node in ast.walk(tree)):
        raise ActionBenchError(f"Action {procedure_id} must call the brokered LLM")
    return schema


def _package_hash(destination: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(destination.rglob("*")):
        if file.is_file() and file.name != "package.json":
            digest.update(file.relative_to(destination).as_posix().encode()); digest.update(file.read_bytes())
    return digest.hexdigest()


def create_package(broker: Broker, episode_id: str, family: Family, replica: int, kind: str, destination: Path, feedback: list[dict] | None = None, base_skill_md: str | None = None, *, revision: int = 0, previous_package: Path | None = None) -> str:
    demos = [{"path": str(path), "content": path.read_text() if path.is_file() else "directory"} for path in family.demonstrations]
    development = _development_prompt_examples(family)
    previous = None
    if previous_package:
        previous = {file.relative_to(previous_package).as_posix(): file.read_text()[:12000] for file in previous_package.rglob("*") if file.is_file() and file.name in {"SKILL.md", "main.py", "procedure.json"}}
    prompt = json.dumps({"family": family.id, "brief": family.creator_brief, "demonstrations": demos, "development_examples": development, "replica": replica, "revision": revision, "base_skill_md": base_skill_md, "previous_package": previous, "previous_development_feedback": feedback or []}, sort_keys=True)
    if destination.exists():
        saved = destination / "package.json"
        if saved.is_file():
            metadata = json.loads(saved.read_text())
            if metadata.get("family") == family.id and metadata.get("replica") == replica and metadata.get("kind") == kind and metadata.get("hash") == _package_hash(destination):
                return metadata["hash"]
        raise ActionBenchError(f"Incomplete package directory needs manual review: {destination}")
    instructions = {"skill": SKILL_PROMPT, "skill_script": SCRIPT_PROMPT, "action": ACTION_PROMPT}[kind]
    available = broker.remaining_output_tokens(episode_id) if hasattr(broker, "remaining_output_tokens") else 4096
    if available < 512:
        raise BudgetExceeded(f"Creation output budget exhausted before revision {revision}")
    result = broker.call(episode_id, f"package-{kind}-draft-{revision}", instructions, prompt,
                         min(6000, available), response_format=package_format(kind))
    try:
        package = _decode(result.text, kind)
        if kind in {"skill_script", "action"}:
            if not isinstance(base_skill_md, str) or not base_skill_md.strip(): raise ActionBenchError("Paired skill is missing")
            if not package["procedures"]: raise ActionBenchError("Procedure package must expose at least one procedure")
            if len(package["procedures"]) > 2: raise ActionBenchError("Procedure package exceeds the two-procedure limit")
            seen = set()
            for procedure in package["procedures"]:
                _validate_procedure(procedure, kind)
                if procedure["id"] in seen: raise ActionBenchError("Procedure ids must be unique")
                seen.add(procedure["id"])
    except ActionBenchError:
        if getattr(result, "request_id", None) and hasattr(broker, "store"):
            broker.store.mark_output_validation(result.request_id, "invalid")
        raise
    if getattr(result, "request_id", None) and hasattr(broker, "store"):
        broker.store.mark_output_validation(result.request_id, "accepted")
    staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.building")
    staging.mkdir(parents=True, exist_ok=False)
    (staging / "SKILL.md").write_text(package["skill_md"] if kind == "skill" else base_skill_md)
    if kind in {"skill_script", "action"}:
        procedures = staging / "procedures"; procedures.mkdir()
        from . import action_sdk
        sdk = Path(action_sdk.__file__).read_text()
        for procedure in package["procedures"]:
            root = procedures / procedure["id"]; root.mkdir()
            (root / "main.py").write_text(procedure["code"])
            if kind == "action": (root / "action_sdk.py").write_text(sdk)
            (root / "procedure.json").write_text(json.dumps({"id": procedure["id"], "description": procedure["description"],
                "input_schema": json.loads(procedure["input_schema_json"]), "command": ["python", "/action/main.py"]}, sort_keys=True))
    digest = _package_hash(staging)
    (staging / "package.json").write_text(json.dumps({"family": family.id, "replica": replica, "kind": kind, "hash": digest}, indent=2))
    os.replace(staging, destination)
    return digest
