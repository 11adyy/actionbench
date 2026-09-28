from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .broker import Broker
from .errors import ActionBenchError
from .runner import ActionRunner


AGENT_INSTRUCTIONS = """Solve the task using the supplied context. Reply only as a JSON object. Use {\"type\":\"final\",\"answer\":string} to finish. If actions are available, you may use {\"type\":\"action\",\"action_id\":string,\"input\":object}. If temporary programmatic reasoning is allowed, you may use {\"type\":\"python\",\"code\":string,\"input\":object}; code must use ActionContext and ctx.emit. Never claim a tool output you did not receive."""


class AgentRunner:
    def __init__(self, broker: Broker, actions: ActionRunner):
        self.broker, self.actions = broker, actions

    def run(self, episode_id: str, task_input: str, condition: str, skill_dir: Path | None) -> str:
        skill = skill_dir.joinpath("SKILL.md").read_text() if skill_dir else ""
        catalog = []
        if condition == "action" and skill_dir:
            for manifest in skill_dir.glob("actions/*/action.json"):
                catalog.append(json.loads(manifest.read_text())["id"])
        context = {"task": task_input, "condition": condition, "skill": skill, "actions": catalog, "observations": []}
        for index in range(self.broker.config.budget.max_llm_calls):
            result = self.broker.call(episode_id, f"agent-{index}", AGENT_INSTRUCTIONS, json.dumps(context), min(2048, self.broker.config.budget.max_output_tokens))
            try: decision = json.loads(result.text)
            except json.JSONDecodeError as exc: raise ActionBenchError(f"Agent protocol returned non-JSON: {result.text[:300]}") from exc
            if decision.get("type") == "final" and isinstance(decision.get("answer"), str): return decision["answer"]
            if decision.get("type") == "action" and condition == "action" and decision.get("action_id") in catalog:
                root = skill_dir / "actions" / decision["action_id"]
                output = self.actions.run(episode_id, root, json.loads((root / "action.json").read_text())["command"], decision.get("input") or {})
                context["observations"].append({"action": decision["action_id"], "output": output}); continue
            if decision.get("type") == "python" and condition == "improvised":
                if "from action_sdk import ActionContext" not in decision.get("code", ""):
                    raise ActionBenchError("Improvised code must use the action protocol")
                with tempfile.TemporaryDirectory(prefix="actionbench-improv-") as temp:
                    root = Path(temp); (root / "main.py").write_text(decision.get("code", "")); (root / "action.json").write_text(json.dumps({"id": f"improvised-{index}", "command": ["python", "/action/main.py"]}))
                    from . import action_sdk
                    (root / "action_sdk.py").write_text(Path(action_sdk.__file__).read_text())
                    output = self.actions.run(episode_id, root, ["python", "/action/main.py"], decision.get("input") or {})
                    context["observations"].append({"python": True, "output": output}); continue
            raise ActionBenchError(f"Agent requested unavailable or malformed operation: {decision}")
        raise ActionBenchError("Agent exhausted its call budget without a final answer")
