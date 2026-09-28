"""SDK exposed inside action containers. It never contains a provider credential."""
from __future__ import annotations

import json
import sys
from typing import Any


class ActionContext:
    def __init__(self, input_data: dict[str, Any]):
        self.input = input_data
        self._sequence = 0

    def call_llm(self, prompt: str, *, instructions: str = "", max_output_tokens: int = 1024) -> str:
        self._sequence += 1
        message = {"kind": "llm_request", "step": f"action-{self._sequence}", "prompt": prompt, "instructions": instructions, "max_output_tokens": max_output_tokens}
        print(json.dumps(message), flush=True)
        reply = json.loads(sys.stdin.readline())
        if reply.get("kind") == "error":
            raise RuntimeError(reply["message"])
        if reply.get("kind") != "llm_response":
            raise RuntimeError("Broker protocol violation")
        return reply["text"]

    def emit(self, output: dict[str, Any]) -> None:
        print(json.dumps({"kind": "result", "output": output}), flush=True)
