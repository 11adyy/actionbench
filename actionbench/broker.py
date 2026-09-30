from __future__ import annotations

import json
import hashlib
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from .config import Config, api_key
from .errors import BudgetExceeded, CampaignBudgetExceeded, ConfigurationError, InfrastructureError, ProviderOutputError, ProviderRejectedError, UnknownProviderOutcome
from .store import Store


@dataclass(frozen=True)
class ModelResult:
    text: str
    raw: dict[str, Any]
    provider_request_id: str | None
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    actual_usd: float
    request_id: str | None = None


class OpenAIResponsesClient:
    """Small dependency-free client for a real OpenAI-compatible Responses endpoint."""

    def __init__(self, config: Config):
        if config.provider.kind != "openai_responses":
            raise ConfigurationError(f"Unsupported provider kind: {config.provider.kind}")
        self.config = config

    def request(self, payload: dict, *, timeout_seconds: float = 180) -> dict:
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            self.config.provider.base_url.rstrip("/") + "/responses",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {api_key(self.config)}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=min(180, max(.001, timeout_seconds))) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1000]
            if exc.code >= 500:
                raise UnknownProviderOutcome(f"Provider server error after submission ({exc.code}): {detail}") from exc
            try: error_code = (json.loads(detail).get("error") or {}).get("code")
            except (json.JSONDecodeError, AttributeError, TypeError): error_code = None
            if exc.code == 400 and error_code == "invalid_prompt":
                raise ProviderRejectedError(f"Provider rejected prompt ({exc.code}): {detail}") from exc
            raise InfrastructureError(f"Provider rejected request ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise UnknownProviderOutcome(f"Network outcome is unknown: {exc.reason}") from exc
        except (TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            raise UnknownProviderOutcome(f"Provider response could not be confirmed: {exc}") from exc


class Broker:
    def __init__(self, config: Config, store: Store):
        self.config, self.store = config, store
        self.client = OpenAIResponsesClient(config)

    def estimate_usd(self, input_tokens: int, max_output_tokens: int) -> float:
        p = self.config.provider
        input_price = max(p.input_usd_per_million, p.cache_write_usd_per_million or 0)
        return (input_tokens * input_price + max_output_tokens * p.output_usd_per_million) / 1_000_000

    def remaining_output_tokens(self, episode_id: str) -> int:
        return max(0, self.config.budget.max_output_tokens - self.store.episode_limits(episode_id)[2])

    def _usage(self, raw: dict) -> dict[str, int]:
        if not isinstance(raw, dict) or not isinstance(raw.get("usage"), dict):
            raise UnknownProviderOutcome("Provider response lacks auditable token usage")
        usage = raw["usage"]
        details = usage.get("input_tokens_details") or {}
        try:
            tokens = {"input_tokens": int(usage["input_tokens"]), "cached_input_tokens": int(details.get("cached_tokens", 0)), "cache_write_tokens": int(details.get("cache_write_tokens", 0)), "output_tokens": int(usage["output_tokens"])}
        except (KeyError, TypeError, ValueError) as exc:
            raise UnknownProviderOutcome("Provider response has malformed token usage") from exc
        if tokens["input_tokens"] < 1 or tokens["output_tokens"] < 0 or min(tokens["cached_input_tokens"], tokens["cache_write_tokens"]) < 0 or tokens["cached_input_tokens"] + tokens["cache_write_tokens"] > tokens["input_tokens"]:
            raise UnknownProviderOutcome("Provider response has impossible token usage")
        return tokens

    def _cost(self, usage: dict[str, int]) -> float:
        p = self.config.provider
        writes = usage.get("cache_write_tokens", 0)
        uncached = usage["input_tokens"] - usage["cached_input_tokens"] - writes
        write_price = p.cache_write_usd_per_million if p.cache_write_usd_per_million is not None else p.input_usd_per_million
        return (uncached * p.input_usd_per_million + writes * write_price + usage["cached_input_tokens"] * p.cached_input_usd_per_million + usage["output_tokens"] * p.output_usd_per_million) / 1_000_000

    @staticmethod
    def _text(raw: dict) -> str:
        if isinstance(raw.get("output_text"), str):
            return raw["output_text"]
        chunks: list[str] = []
        for item in raw.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"}:
                    chunks.append(content.get("text", ""))
        return "".join(chunks)

    def _checked_result(self, result: ModelResult) -> ModelResult:
        raw = result.raw
        status = raw.get("status")
        if status not in (None, "completed"):
            if result.request_id: self.store.mark_output_validation(result.request_id, "invalid")
            reason = (raw.get("incomplete_details") or {}).get("reason")
            raise ProviderOutputError(f"Provider response {result.provider_request_id} has status {status}: {reason or 'no reason'}")
        if any(content.get("type") == "refusal" for item in raw.get("output", [])
               if isinstance(item, dict) for content in item.get("content", []) if isinstance(content, dict)):
            if result.request_id: self.store.mark_output_validation(result.request_id, "invalid")
            raise ProviderOutputError(f"Provider response {result.provider_request_id} contains a refusal")
        if result.request_id: self.store.mark_output_validation(result.request_id, "provider_complete")
        return result

    def _complete(self, request_id: str, raw: dict) -> ModelResult:
        try:
            usage = self._usage(raw)
        except UnknownProviderOutcome as exc:
            self.store.preserve_unpriced_response(request_id, raw, str(exc))
            raise
        actual = self._cost(usage)
        reservation = self.store.conn.execute("SELECT reserved_usd,reserved_input_tokens FROM requests WHERE request_id=?", (request_id,)).fetchone()
        self.store.complete_request(request_id, raw.get("id"), raw, usage, actual)
        if reservation and (actual > reservation["reserved_usd"] or usage["input_tokens"] > reservation["reserved_input_tokens"]):
            self.store.event(None, "provider_usage_exceeded_reservation", {"request_id": request_id, "reserved_usd": reservation["reserved_usd"], "actual_usd": actual, "reserved_input_tokens": reservation["reserved_input_tokens"], "actual_input_tokens": usage["input_tokens"]})
        return ModelResult(self._text(raw), raw, raw.get("id"), usage["input_tokens"], usage["cached_input_tokens"], usage["output_tokens"], actual, request_id)

    def call(self, episode_id: str, request_key: str, instructions: str, input_text: str, max_output_tokens: int, *, timeout_seconds: float | None = None, response_format: dict | None = None) -> ModelResult:
        """Issue or recover a request. Reuse is allowed only for byte-identical payloads."""
        payload = {"model": self.config.provider.model, "instructions": instructions, "input": input_text, "max_output_tokens": max_output_tokens, "store": False}
        if self.config.provider.reasoning_effort is not None:
            payload["reasoning"] = {"effort": self.config.provider.reasoning_effort}
        if response_format is not None:
            payload["text"] = {"format": response_format}
        request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        existing = self.store.request_for(episode_id, request_key, request_hash)
        if existing:
            if existing["state"] == "completed":
                raw = json.loads(existing["response_json"])
                return self._checked_result(ModelResult(self._text(raw), raw, existing["provider_request_id"], existing["input_tokens"], existing["cached_input_tokens"], existing["output_tokens"], existing["actual_usd"], existing["request_id"]))
            if existing["state"] == "unknown_outcome":
                raise UnknownProviderOutcome(f"Request {request_key} has unknown outcome and must be manually resolved")
            if existing["state"] == "submitted":
                self.store.unknown_request(existing["request_id"], "Coordinator restarted after request submission")
                raise UnknownProviderOutcome(f"Request {request_key} was submitted before interruption and is unknown")
            if existing["state"] == "rejected":
                raise ProviderRejectedError(f"Request {request_key} was already rejected by the provider; do not resend the same payload")
            if existing["state"] == "reserved":
                # Reservation is durably committed before the API call. It is safe
                # to continue it because no provider request has been sent yet.
                request_id = existing["request_id"]
                self.store.mark_submitted(request_id)
                try:
                    raw = self.client.request(payload, timeout_seconds=timeout_seconds) if timeout_seconds is not None else self.client.request(payload)
                except UnknownProviderOutcome as exc:
                    self.store.unknown_request(request_id, str(exc)); raise
                except ProviderRejectedError as exc:
                    self.store.reject_request(request_id, str(exc)); raise
                except (ConfigurationError, InfrastructureError) as exc:
                    self.store.reject_request(request_id, str(exc)); raise
                return self._checked_result(self._complete(request_id, raw))
            raise UnknownProviderOutcome(f"Request {request_key} has unsupported stored state {existing['state']}")
        if max_output_tokens < 1 or max_output_tokens > self.config.budget.max_output_tokens:
            raise BudgetExceeded("Requested output tokens exceed the campaign limit")
        # Byte-level upper bound for byte-oriented model tokenizers, plus a
        # fixed allowance for provider message framing. Unknown tokenizers
        # still require an explicit compatible-provider claim in the paper.
        estimated_input = len(instructions.encode()) + len(input_text.encode()) + len(json.dumps(response_format).encode() if response_format is not None else b"") + 1024
        call_count, used_input, used_output = self.store.episode_limits(episode_id)
        if call_count >= self.config.budget.max_llm_calls:
            raise BudgetExceeded("Episode call limit would be exceeded")
        if used_input + estimated_input > self.config.budget.max_input_tokens:
            raise BudgetExceeded("Episode input-token budget would be exceeded")
        if used_output + max_output_tokens > self.config.budget.max_output_tokens:
            raise BudgetExceeded("Episode output-token budget would be exceeded")
        reserve = self.estimate_usd(estimated_input, max_output_tokens)
        if self.store.campaign_spend(self.config.campaign) + reserve > self.store.budget_ceiling(self.config):
            raise CampaignBudgetExceeded("Campaign dollar budget would be exceeded")
        request_id = str(uuid.uuid4())
        self.store.reserve_request(request_id, episode_id, request_key, request_hash, payload, reserve, estimated_input)
        self.store.mark_submitted(request_id)
        try:
            raw = self.client.request(payload, timeout_seconds=timeout_seconds) if timeout_seconds is not None else self.client.request(payload)
        except UnknownProviderOutcome as exc:
            self.store.unknown_request(request_id, str(exc))
            raise
        except ProviderRejectedError as exc:
            self.store.reject_request(request_id, str(exc))
            raise
        except (ConfigurationError, InfrastructureError) as exc:
            self.store.reject_request(request_id, str(exc))
            raise
        return self._checked_result(self._complete(request_id, raw))
