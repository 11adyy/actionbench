from __future__ import annotations

import json
import uuid

from .broker import Broker
from .errors import ActionBenchError


def dispatch(args, config, store) -> int:
    if args.command == "live-check":
        episode_id = f"live-check-{uuid.uuid4()}"
        store.create_episode(episode_id, config.campaign, episode_id, "integration", "plain", 0)
        result = Broker(config, store).call(episode_id, "healthcheck", "Reply with exactly OK.", "Health check.", 16)
        if result.text.strip() != "OK":
            raise ActionBenchError(f"Unexpected provider response: {result.text!r}")
        print(json.dumps({"provider_request_id": result.provider_request_id, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens, "actual_usd": result.actual_usd}, indent=2))
        return 0
    raise ActionBenchError(f"Command '{args.command}' is not implemented in this repository revision")
