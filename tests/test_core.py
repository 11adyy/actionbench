import json
import tempfile
import unittest
from pathlib import Path

from actionbench.config import load_config
from actionbench.report import build_report
from actionbench.store import Store


class CoreTests(unittest.TestCase):
    def make(self, root: Path, campaign="c"):
        raw = json.loads((Path(__file__).parents[1] / "config.example.json").read_text())
        raw.update({"campaign": campaign, "dataset_root": "data", "artifact_root": "artifacts"})
        path = root / "config.json"; path.write_text(json.dumps(raw))
        config = load_config(path); store = Store(config.db_path); store.ensure_campaign(config)
        return config, store

    def test_request_identity_includes_payload_hash(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "plain", 0)
            store.reserve_request("r-a", "e", "step", "hash-a", {"input": "A"}, .1, 10)
            self.assertIsNotNone(store.request_for("e", "step", "hash-a"))
            self.assertIsNone(store.request_for("e", "step", "hash-b"))

    def test_report_keeps_failed_episodes_in_denominator(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            for number in range(10):
                episode = f"e-{number}"; store.create_episode(episode, config.campaign, f"task-{number}", "code", "action", 0)
                store.set_episode(episode, "completed" if number == 0 else "failed", retryable=False)
            store.save_evaluation("e-0", "code", {"primary": 1})
            group = build_report(config, store)["groups"]["code:action"]
            self.assertEqual(group["mean_primary_among_scored"], 1)
            self.assertEqual(group["primary_with_failures_as_zero"], .1)
            self.assertEqual(group["execution_failed"], 9)

    def test_report_pairs_action_against_skill_by_task_and_replica(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            for condition, score in (("skill", 0), ("action", 1)):
                episode = f"{condition}-e"; store.create_episode(episode, config.campaign, "task", "code", condition, 0, condition)
                store.set_episode(episode, "completed", retryable=False); store.save_evaluation(episode, "code", {"primary": score})
            comparison = build_report(config, store)["paired_comparisons"]["code:action_minus_skill"]
            self.assertEqual(comparison["quality"]["n"], 1)
            self.assertEqual(comparison["quality"]["mean_delta"], 1)

    def test_resume_includes_retryable_failures(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("retry", config.campaign, "t", "f", "plain", 0)
            store.create_episode("stop", config.campaign, "u", "f", "plain", 0)
            store.set_episode("retry", "failed", retryable=True); store.set_episode("stop", "failed", retryable=False)
            self.assertEqual([row["episode_id"] for row in store.resumable_episodes(config.campaign)], ["retry"])

    def test_action_run_identity_contains_input_hash(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "action", 0)
            first = store.create_action_run("one", "e", "invoke-0", "extract", "input-a", "/tmp/a")
            second = store.create_action_run("two", "e", "invoke-0", "extract", "input-b", "/tmp/b")
            self.assertEqual(first["action_run_id"], "one")
            self.assertEqual(second["action_run_id"], "two")


if __name__ == "__main__": unittest.main()
