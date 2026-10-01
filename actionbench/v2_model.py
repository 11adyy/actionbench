"""One metered model for the Deep Agent and for model calls inside skill graphs."""
from __future__ import annotations

import contextlib
import contextvars
import uuid
from dataclasses import dataclass

from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI

from .v2_store import Ledger


@dataclass(frozen=True)
class Scope:
    episode: str
    step: str
    episode_limit: float


_scope: contextvars.ContextVar[Scope | None] = contextvars.ContextVar("actionbench_v2_scope", default=None)


@contextlib.contextmanager
def model_scope(episode: str, step: str, episode_limit: float):
    token = _scope.set(Scope(episode, step, episode_limit))
    try:
        yield
    finally:
        _scope.reset(token)


class Meter(BaseCallbackHandler):
    def __init__(self, ledger: Ledger, campaign_limit: float, input_price: float, cached_price: float, output_price: float, max_output_tokens: int = 1024):
        self.ledger = ledger
        self.campaign_limit = campaign_limit
        self.input_price = input_price
        self.cached_price = cached_price
        self.output_price = output_price
        self.max_output_tokens = max_output_tokens
        self.pending: dict[str, str] = {}

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        scope = _scope.get()
        if scope is None:
            raise ValueError("Model call has no campaign scope")
        # Count the prompt conservatively, including Unicode bytes. Tool schemas
        # add tokens, so the reservation includes a fixed 2,048-token allowance.
        prompt_bytes = sum(len(str(message).encode()) for group in messages for message in group)
        estimated_input = int(prompt_bytes / 2) + 2048
        reserved = (estimated_input * self.input_price + self.max_output_tokens * self.output_price) / 1_000_000
        call_id = uuid.uuid4().hex
        self.ledger.reserve(call_id, scope.episode, scope.step, reserved, scope.episode_limit, self.campaign_limit)
        self.pending[str(run_id)] = call_id

    def on_llm_end(self, response, *, run_id, **kwargs):
        call_id = self.pending.pop(str(run_id))
        message = response.generations[0][0].message
        usage = message.usage_metadata or {}
        if not usage:
            # Submitted but unpriced: keep reservation and block resume.
            return
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        details = usage.get("input_token_details") or {}
        cached = int(details.get("cache_read", 0))
        if input_tokens < 1 or output_tokens < 0 or cached < 0 or cached > input_tokens:
            return
        actual = ((input_tokens-cached)*self.input_price + cached*self.cached_price + output_tokens*self.output_price) / 1_000_000
        self.ledger.complete(call_id,input_tokens=input_tokens,cached_tokens=cached,output_tokens=output_tokens,actual_usd=actual,provider_id=message.id or response.llm_output.get("id") if response.llm_output else message.id)

    def on_llm_error(self, error, *, run_id, **kwargs):
        call_id = self.pending.pop(str(run_id), None)
        if call_id and getattr(error, "status_code", None) in (400, 401, 403):
            self.ledger.reject(call_id, str(error))
        # Timeouts and transport errors remain submitted: provider outcome unknown.


def make_model(provider: dict, meter: Meter) -> ChatOpenAI:
    if provider.get("reasoning_effort") != "none":
        raise ValueError("v2 requires frozen reasoning_effort=none")
    return ChatOpenAI(
        model=provider["model"],
        reasoning_effort="none",
        use_responses_api=True,
        max_completion_tokens=meter.max_output_tokens,
        max_retries=0,
        callbacks=[meter],
        api_key=provider.get("api_key_env_value"),
    )
