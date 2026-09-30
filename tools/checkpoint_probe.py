"""Exercise durable request and answer crash windows across hosted runners.

The provider reply is controlled: this tests persistence, never model behavior.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from actionbench.broker import Broker
from actionbench.commands import _read_saved_answer
from actionbench.config import load_config
from actionbench.errors import UnknownProviderOutcome
from actionbench.store import Store


POINTS = ("before_reserve", "after_reserve", "after_submitted", "after_response_received", "after_response_saved")
EXPECTED = (None, "reserved", "submitted", "submitted", "completed")


class ControlledProvider:
    def __init__(self):
        self.calls = 0

    def request(self, payload):
        self.calls += 1
        return {"id": f"controlled-{self.calls}", "status": "completed", "output_text": "OK",
                "usage": {"input_tokens": 20, "output_tokens": 2}}


def request_state(store: Store, episode: str) -> str | None:
    row = store.conn.execute("SELECT state FROM requests WHERE episode_id=?", (episode,)).fetchone()
    return row["state"] if row else None


def prepare(config, store: Store) -> dict:
    evidence = {}
    for point, expected in zip(POINTS, EXPECTED):
        episode = f"{config.campaign}:checkpoint:{point}"
        store.create_episode(episode, config.campaign, point, "integration", "plain", 0)
        broker = Broker(config, store)
        provider = ControlledProvider()
        broker.client = provider
        os.environ["AB_TEST_FAULT_POINT"] = point
        try:
            try: broker.call(episode, "decision", "Return OK", "test input", 4)
            except SystemExit as exc:
                if exc.code != 97: raise
            else:
                raise AssertionError(f"Fault point {point} did not interrupt the request")
        finally:
            os.environ.pop("AB_TEST_FAULT_POINT", None)
        state = request_state(store, episode)
        if state != expected:
            raise AssertionError(f"{point}: expected {expected}, found {state}")
        evidence[point] = {"state_before_restore": state, "controlled_sends": provider.calls}

    answer_episode = f"{config.campaign}:checkpoint:answer"
    store.create_episode(answer_episode, config.campaign, "answer-checkpoint", "integration", "plain", 0)
    answer = config.artifact_root / "answers" / config.campaign / "checkpoint-answer.txt"
    answer.parent.mkdir(parents=True, exist_ok=True)
    answer.write_text("persisted answer")
    os.environ["AB_TEST_FAULT_POINT"] = "after_answer_saved"
    try:
        try: store.save_answer(answer_episode, str(answer))
        except SystemExit as exc:
            if exc.code != 97: raise
        else: raise AssertionError("Answer fault point did not interrupt")
    finally:
        os.environ.pop("AB_TEST_FAULT_POINT", None)
    os.environ["AB_TEST_FAULT_POINT"] = "after_evaluation_saved"
    try:
        try: store.save_evaluation(answer_episode, "controlled", {"primary": 0.5})
        except SystemExit as exc:
            if exc.code != 97: raise
        else: raise AssertionError("Evaluation fault point did not interrupt")
    finally:
        os.environ.pop("AB_TEST_FAULT_POINT", None)
    evidence["answer_and_evaluation"] = "persisted"
    target = config.artifact_root / "checkpoint-probe-before.json"
    target.write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


def verify(config, store: Store) -> dict:
    evidence = {}
    for point, expected in zip(POINTS, EXPECTED):
        episode = f"{config.campaign}:checkpoint:{point}"
        if request_state(store, episode) != expected:
            raise AssertionError(f"Restored state for {point} changed before verification")
        broker = Broker(config, store)
        provider = ControlledProvider()
        broker.client = provider
        if expected == "submitted":
            try: broker.call(episode, "decision", "Return OK", "test input", 4)
            except UnknownProviderOutcome: pass
            else: raise AssertionError(f"{point} was resent despite an ambiguous submitted state")
            if request_state(store, episode) != "unknown_outcome" or provider.calls:
                raise AssertionError(f"{point} did not block without another provider send")
        else:
            result = broker.call(episode, "decision", "Return OK", "test input", 4)
            if result.text != "OK": raise AssertionError(f"{point} did not recover the expected response")
            expected_sends = 0 if expected == "completed" else 1
            if provider.calls != expected_sends:
                raise AssertionError(f"{point} sent {provider.calls}, expected {expected_sends}")
        count = store.conn.execute("SELECT COUNT(*) FROM requests WHERE episode_id=?", (episode,)).fetchone()[0]
        if count != 1: raise AssertionError(f"{point} duplicated its ledger request")
        evidence[point] = {"state_after_restore": request_state(store, episode), "new_controlled_sends": provider.calls}

    answer_episode = f"{config.campaign}:checkpoint:answer"
    answer = config.artifact_root / "answers" / config.campaign / "checkpoint-answer.txt"
    if _read_saved_answer(store.episode(answer_episode), answer) != "persisted answer":
        raise AssertionError("Checkpointed answer changed")
    if json.loads(store.evaluation(answer_episode)["score_json"]) != {"primary": 0.5}:
        raise AssertionError("Checkpointed evaluation changed")
    evidence["answer_and_evaluation"] = "restored_without_rerun"
    target = config.artifact_root / "checkpoint-probe-after.json"
    target.write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "verify"))
    parser.add_argument("--config", default="experiment.json")
    args = parser.parse_args()
    config = load_config(args.config)
    store = Store(config.db_path)
    try:
        store.ensure_campaign(config)
        print(json.dumps(prepare(config, store) if args.mode == "prepare" else verify(config, store), indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
