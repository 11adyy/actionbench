"""Durable crash-window checks against the real SQLite store; provider replies are controlled fixtures."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actionbench.broker import Broker
from actionbench.commands import _read_saved_answer
from actionbench.config import load_config
from actionbench.errors import UnknownProviderOutcome
from actionbench.store import Store


class FaultWindowTests(unittest.TestCase):
    def make(self, root):
        raw = json.loads((Path(__file__).parents[1] / "config.example.json").read_text())
        raw.update({"campaign": "fault-window", "artifact_root": "artifacts", "dataset_root": "data"})
        path = root / "config.json"
        path.write_text(json.dumps(raw))
        config = load_config(path)
        store = Store(config.db_path)
        store.ensure_campaign(config)
        return config, store

    def test_provider_crash_windows_never_duplicate_confirmed_calls(self):
        class Client:
            calls = 0
            def request(self, payload):
                self.calls += 1
                return {"id": f"provider-{self.calls}", "status": "completed", "output_text": "OK",
                        "usage": {"input_tokens": 20, "output_tokens": 2}}

        for point, initial_state, sends_before_crash, sends_after_resume in (
            ("before_reserve", None, 0, 1),
            ("after_reserve", "reserved", 0, 1),
            ("after_submitted", "submitted", 0, 0),
            ("after_response_received", "submitted", 1, 0),
            ("after_response_saved", "completed", 1, 0),
        ):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as d:
                config, store = self.make(Path(d))
                store.create_episode("e", config.campaign, "task", "f", "plain", 0)
                client = Client()
                broker = Broker(config, store)
                broker.client = client
                with patch.dict("os.environ", {"AB_TEST_FAULT_POINT": point}):
                    with self.assertRaises(SystemExit): broker.call("e", "k", "instructions", "input", 4)
                saved = store.conn.execute("SELECT state FROM requests").fetchone()
                self.assertEqual(saved["state"] if saved else None, initial_state)
                self.assertEqual(client.calls, sends_before_crash)
                store.close()
                restored = Store(config.db_path)
                try:
                    resumed = Broker(config, restored)
                    resumed.client = client
                    if initial_state == "submitted":
                        with self.assertRaises(UnknownProviderOutcome): resumed.call("e", "k", "instructions", "input", 4)
                        self.assertEqual(restored.conn.execute("SELECT state FROM requests").fetchone()[0], "unknown_outcome")
                    else:
                        self.assertEqual(resumed.call("e", "k", "instructions", "input", 4).text, "OK")
                    self.assertEqual(client.calls, sends_before_crash + sends_after_resume)
                    self.assertEqual(restored.conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)
                finally:
                    restored.close()

    def test_answer_and_grader_checkpoints_survive_restart(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            store.create_episode("e", config.campaign, "task", "f", "plain", 0)
            answer = Path(d) / "answer.txt"
            answer.write_text("final answer")
            with patch.dict("os.environ", {"AB_TEST_FAULT_POINT": "after_answer_saved"}):
                with self.assertRaises(SystemExit): store.save_answer("e", str(answer))
            store.close()
            store = Store(config.db_path)
            self.assertEqual(_read_saved_answer(store.episode("e"), answer), "final answer")
            with patch.dict("os.environ", {"AB_TEST_FAULT_POINT": "after_evaluation_saved"}):
                with self.assertRaises(SystemExit): store.save_evaluation("e", "official", {"primary": 0.5})
            store.close()
            restored = Store(config.db_path)
            try:
                self.assertEqual(json.loads(restored.evaluation("e")["score_json"]), {"primary": 0.5})
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
