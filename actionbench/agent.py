from __future__ import annotations

import json
from pathlib import Path

from .broker import Broker
from .errors import ActionBenchError
from .runner import ActionRunner


AGENT_INSTRUCTIONS = """Solve the task using only the provided context and tool observations. Reply with exactly one JSON object. Every condition has `code`, which executes a Python JSONL program without network or model access; its code must read one line from stdin and emit {\"kind\":\"result\",\"output\":object}. Use {\"type\":\"code\",\"code\":string,\"input\":object} to invoke it. Use {\"type\":\"final\",\"answer\":string} to deliver the benchmark answer. If the catalog lists actions, use {\"type\":\"action\",\"action_id\":string,\"input\":object}. Only if `llm_code` is listed may you use {\"type\":\"llm_code\",\"code\":string,\"input\":object}; that program imports ActionContext from action_sdk and calls ctx.emit. Never invent tool results."""


class AgentRunner:
    def __init__(self, broker: Broker, tools: ActionRunner):
        self.broker, self.tools = broker, tools

    def run(self, episode_id: str, task_input: str, condition: str, skill_dir: Path | None, action_dir: Path | None) -> str:
        skill = skill_dir.joinpath("SKILL.md").read_text() if skill_dir else ""
        catalog = []
        if action_dir:
            catalog = sorted(json.loads(item.read_text())["id"] for item in action_dir.glob("actions/*/action.json"))
        enabled = ["code"]
        if condition == "improvised": enabled.append("llm_code")
        if condition == "action": enabled.append("action")
        context = {"task": task_input, "skill": skill, "condition": condition, "tools": enabled, "actions": catalog, "observations": []}
        for index in range(self.broker.config.budget.max_llm_calls):
            result = self.broker.call(episode_id, f"agent-decision-{index}", AGENT_INSTRUCTIONS, json.dumps(context, sort_keys=True), min(2048, self.broker.config.budget.max_output_tokens))
            try: decision = json.loads(result.text)
            except json.JSONDecodeError as exc: raise ActionBenchError(f"Agent emitted non-JSON: {result.text[:300]}") from exc
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
            if kind == "action" and condition == "action" and decision.get("action_id") in catalog and action_dir:
                root = action_dir / "actions" / decision["action_id"]
                output = self.tools.run(episode_id, root, f"action-{index}", input_data)
                context["observations"].append({"tool": "action", "action_id": decision["action_id"], "output": output}); continue
            raise ActionBenchError(f"Unavailable or malformed tool request: {decision}")
        raise ActionBenchError("Agent exhausted its call budget without a final answer")
