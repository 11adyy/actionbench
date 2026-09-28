from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .broker import Broker
from .errors import ActionBenchError
from .runner import ActionRunner


AGENT_INSTRUCTIONS = """Solve the task using only the provided context and tool observations. Reply with exactly one JSON object, without Markdown. Every condition has `code`, which executes a Python JSONL program without network or model access; its code must read one line from stdin and emit {\"kind\":\"result\",\"output\":object}. Use {\"type\":\"code\",\"code\":string,\"input\":object} to invoke it. Use {\"type\":\"final\",\"answer\":string} to deliver the benchmark answer. If the catalog lists reusable procedures, follow each procedure's description and input_schema, then use {\"type\":\"procedure\",\"procedure_id\":string,\"input\":object}. Only if `llm_code` is listed may you use {\"type\":\"llm_code\",\"code\":string,\"input\":object}; its complete Python source must follow this protocol: import json,sys; from action_sdk import ActionContext; ctx=ActionContext(json.loads(sys.stdin.readline())[\"input\"]); text=ctx.call_llm(prompt, instructions=..., max_output_tokens=...); ctx.emit({\"text\":text}). `prompt` must be an actual string derived from ctx.input. Never invent tool results."""


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
        context = {"task": task_input, "skill": skill, "condition": condition, "tools": enabled, "procedures": catalog, "observations": []}
        for index in range(self.broker.config.budget.max_llm_calls):
            available = self.broker.remaining_output_tokens(episode_id) if hasattr(self.broker, "remaining_output_tokens") else self.broker.config.budget.max_output_tokens
            result = self.broker.call(episode_id, f"agent-decision-{index}", AGENT_INSTRUCTIONS, json.dumps(context, sort_keys=True), min(2048, available))
            try: decision = json.loads(result.text)
            except json.JSONDecodeError as exc: raise ActionBenchError(f"Agent emitted non-JSON: {result.text[:300]}") from exc
            if not isinstance(decision, dict): raise ActionBenchError("Agent decision must be a JSON object")
            kind = decision.get("type")
            if kind == "final" and isinstance(decision.get("answer"), str): return decision["answer"]
            input_data = decision.get("input") or {}
            if not isinstance(input_data, dict): raise ActionBenchError("Tool input must be an object")
            if kind == "code":
                output = self.tools.run_plain_program(episode_id, f"code-{index}", str(decision.get("code", "")), input_data)
                context["observations"].append({"tool": "code", "output": output}); continue
            if kind == "llm_code" and condition == "improvised":
                output = self.tools.run_ephemeral_llm_program(episode_id, f"llm-code-{index}", str(decision.get("code", "")), input_data)
                context["observations"].append({"tool": "llm_code", "output": output}); continue
            procedure_id = decision.get("procedure_id")
            known = {item.get("id") for item in catalog}
            if kind == "procedure" and condition in {"skill_script", "action"} and procedure_id in known and package_dir:
                schema = next(item["input_schema"] for item in catalog if item["id"] == procedure_id)
                try: Draft202012Validator(schema).validate(input_data)
                except ValidationError as exc: raise ActionBenchError(f"Procedure {procedure_id} input violates schema: {exc.message}") from exc
                root = package_dir / "procedures" / procedure_id
                output = self.tools.run(episode_id, root, f"procedure-{index}", input_data, allow_llm=condition == "action")
                context["observations"].append({"tool": "procedure", "procedure_id": procedure_id, "output": output}); continue
            raise ActionBenchError(f"Unavailable or malformed tool request: {decision}")
        raise ActionBenchError("Agent exhausted its call budget without a final answer")
