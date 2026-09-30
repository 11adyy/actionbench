from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .broker import Broker
from .contracts import decision_format
from .errors import ActionBenchError, BudgetExceeded, CampaignBudgetExceeded, ConfigurationError, InfrastructureError, ProviderOutputError, UnknownProviderOutcome
from .runner import ActionRunner


AGENT_INSTRUCTIONS = """You are an agent solving a benchmark task. Return only the decision object required by the response schema. The outer decision object is the harness protocol; its `answer` string is the actual benchmark submission. For a Python programming task, put complete Python source in `answer`. For a question-answering task, put the required answer object serialized as JSON in `answer`. Do not put the harness decision object itself in `answer`. All decision fields must be present; use null for unused fields. For `code`, write a Python JSONL program without network or model access: read one input line and emit {\"kind\":\"result\",\"output\":object}. For `procedure`, choose a listed ID and provide `input_json` as a JSON object string matching its schema. For `llm_code`, write complete Python code: import json,sys; from action_sdk import ActionContext; ctx=ActionContext(json.loads(sys.stdin.readline())['input']); text=ctx.call_llm(prompt, instructions='', max_output_tokens=512); ctx.emit({'text':text}). Derive prompt from ctx.input. Never invent tool observations. Only choose a tool listed in `tools`; return a final answer when ready."""


class AgentRunner:
    def __init__(self, broker: Broker, tools: ActionRunner):
        self.broker, self.tools = broker, tools

    def run(self, episode_id: str, task_input: str, condition: str, skill_dir: Path | None, action_dir: Path | None) -> str:
        package_dir = action_dir or skill_dir
        skill = package_dir.joinpath("SKILL.md").read_text() if package_dir else ""
        catalog = []
        if package_dir:
            catalog = sorted(
                (json.loads(item.read_text()) for item in package_dir.glob("procedures/*/procedure.json")),
                key=lambda procedure: procedure["id"],
            )
        enabled = ["code"]
        if condition == "improvised": enabled.append("llm_code")
        if condition in {"skill_script", "action"}: enabled.append("procedure")
        try:
            parsed_task = json.loads(task_input)
        except (TypeError, json.JSONDecodeError):
            parsed_task = task_input
        context = {"task": parsed_task, "skill": skill, "condition": condition, "tools": enabled, "procedures": catalog, "observations": [], "protocol_feedback": []}
        repairs = 0
        last_protocol_error = None
        for index in range(self.broker.config.budget.max_llm_calls):
            available = self.broker.remaining_output_tokens(episode_id) if hasattr(self.broker, "remaining_output_tokens") else self.broker.config.budget.max_output_tokens
            try:
                result = self.broker.call(episode_id, f"agent-decision-{index}", AGENT_INSTRUCTIONS,
                                          json.dumps(context, sort_keys=True), min(2048, available),
                                          response_format=decision_format(enabled))
                try:
                    decision = json.loads(result.text)
                except json.JSONDecodeError as exc:
                    raise ActionBenchError("Agent decision was not valid JSON") from exc
                if not isinstance(decision, dict) or set(decision) != {"type", "answer", "code", "procedure_id", "input_json"}:
                    raise ActionBenchError("Agent decision did not match the required envelope")
                kind = decision["type"]
                if kind == "final":
                    if any(decision[field] is not None for field in ("code", "procedure_id", "input_json")):
                        raise ActionBenchError("Final decision must leave tool fields null")
                    if not isinstance(decision["answer"], str) or not decision["answer"].strip():
                        raise ActionBenchError("Final decision needs a nonempty answer")
                    return decision["answer"]
                if kind not in enabled:
                    raise ActionBenchError(f"Tool {kind} is unavailable in {condition}")
                if decision["answer"] is not None:
                    raise ActionBenchError("Tool decision must leave answer null")
                try:
                    input_data = json.loads(decision["input_json"] or "{}")
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ActionBenchError("Tool input_json must encode an object") from exc
                if not isinstance(input_data, dict):
                    raise ActionBenchError("Tool input_json must encode an object")
                if kind in {"code", "llm_code"}:
                    if decision["procedure_id"] is not None:
                        raise ActionBenchError("Code decision must leave procedure_id null")
                    if not isinstance(decision["code"], str) or not decision["code"].strip():
                        raise ActionBenchError(f"{kind} requires nonempty code")
                    if kind == "code":
                        output = self.tools.run_plain_program(episode_id, f"code-{index}", decision["code"], input_data)
                    else:
                        output = self.tools.run_ephemeral_llm_program(episode_id, f"llm-code-{index}", decision["code"], input_data)
                    context["observations"].append({"tool": kind, "output": output})
                    continue
                procedure_id = decision["procedure_id"]
                if decision["code"] is not None:
                    raise ActionBenchError("Procedure decision must leave code null")
                known = {item.get("id") for item in catalog}
                if kind != "procedure" or procedure_id not in known or not package_dir:
                    raise ActionBenchError(f"Unknown procedure: {procedure_id}")
                schema = next(item["input_schema"] for item in catalog if item["id"] == procedure_id)
                try:
                    Draft202012Validator(schema).validate(input_data)
                except ValidationError as exc:
                    raise ActionBenchError(f"Procedure {procedure_id} input violates schema: {exc.message}") from exc
                root = package_dir / "procedures" / procedure_id
                output = self.tools.run(episode_id, root, f"procedure-{index}", input_data, allow_llm=condition == "action")
                context["observations"].append({"tool": "procedure", "procedure_id": procedure_id, "output": output})
            except (CampaignBudgetExceeded, BudgetExceeded, UnknownProviderOutcome, InfrastructureError, ConfigurationError):
                raise
            except (ActionBenchError, ProviderOutputError) as exc:
                repairs += 1
                last_protocol_error = str(exc)
                if hasattr(self.broker, "store"):
                    self.broker.store.event(episode_id, "agent_protocol_repair", {"attempt": index, "error": str(exc)[:300]})
                if repairs > 2:
                    raise ActionBenchError(f"Agent protocol failed after two repairs: {exc}") from exc
                context["protocol_feedback"].append({"attempt": index, "error": str(exc)[:300], "instruction": "Return the required decision object; choose only an available tool."})
        raise ActionBenchError(f"Agent exhausted its call budget without a final answer; last protocol error: {last_protocol_error}" if last_protocol_error else "Agent exhausted its call budget without a final answer")
