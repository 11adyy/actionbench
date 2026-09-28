import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from actionbench.agent import AgentRunner
from actionbench.commands import _validate_on_development
from actionbench.config import load_config
from actionbench.broker import Broker
from actionbench.errors import UnknownProviderOutcome
from actionbench.report import build_report
from actionbench.runner import ActionRunner
from actionbench.skill_creator import create_package
from actionbench.store import Store
from actionbench.statistics import clustered_paired_bootstrap


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

    def test_reserved_request_can_resume_without_duplicate_reservation(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "plain", 0)
            payload = {"model": config.provider.model, "instructions": "i", "input": "x", "max_output_tokens": 4, "store": False}
            import hashlib
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            store.reserve_request("r", "e", "k", digest, payload, 0, 1)
            broker = Broker(config, store)
            broker.client = type("Client", (), {"request": lambda self, _: {"id": "p", "output_text": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}})()
            self.assertEqual(broker.call("e", "k", "i", "x", 4).text, "ok")
            self.assertEqual(store.request_for("e", "k", digest)["state"], "completed")

    def test_submitted_request_becomes_manual_review_on_restart(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "plain", 0)
            payload = {"model": config.provider.model, "instructions": "i", "input": "x", "max_output_tokens": 4, "store": False}
            import hashlib
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            store.reserve_request("r", "e", "k", digest, payload, 0, 1); store.mark_submitted("r")
            with self.assertRaises(UnknownProviderOutcome): Broker(config, store).call("e", "k", "i", "x", 4)
            self.assertEqual(store.request_for("e", "k", digest)["state"], "unknown_outcome")

    def test_action_receives_paired_skill_and_usable_procedure_contract(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); package = root / "package"; procedure = package / "procedures" / "extract"
            procedure.mkdir(parents=True); (package / "SKILL.md").write_text("Follow the exact answer contract.")
            (procedure / "procedure.json").write_text(json.dumps({"id": "extract", "description": "Extract named entities.", "input_schema": {"type": "object", "required": ["text"]}, "command": ["python", "/action/main.py"]}))
            class CaptureBroker:
                config = SimpleNamespace(budget=SimpleNamespace(max_llm_calls=1, max_output_tokens=64))
                def call(self, *_args):
                    self.context = json.loads(_args[3])
                    return SimpleNamespace(text='{"type":"final","answer":"done"}')
            broker = CaptureBroker()
            self.assertEqual(AgentRunner(broker, None).run("e", "task", "action", None, package), "done")
            self.assertEqual(broker.context["skill"], "Follow the exact answer contract.")
            self.assertEqual(broker.context["procedures"][0]["input_schema"]["required"], ["text"])
            self.assertIn("procedure", broker.context["tools"])

    def test_plain_code_runs_at_its_mounted_workspace_path(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "plain", 0)
            runner = ActionRunner(config, SimpleNamespace(store=store))
            observed = {}
            class FakeContainer:
                def execute(self, _episode, _run, _workspace, command, _input, action_dir=None, allow_llm=False):
                    observed.update(command=command, action_dir=action_dir, allow_llm=allow_llm)
                    return {"ok": True}
            runner.container = FakeContainer()
            self.assertEqual(runner.run_plain_program("e", "step", "print('x')", {}), {"ok": True})
            self.assertEqual(observed["command"][0], "python")
            self.assertRegex(observed["command"][1], r"^/workspace/[0-9a-f]+\.py$")
            self.assertIsNone(observed["action_dir"])

    def test_package_write_is_recoverable_after_successful_creation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); config, store = self.make(root); store.create_episode("e", config.campaign, "creation:f:skill:0", "f", "skill", 0)
            family = SimpleNamespace(id="f", creator_brief="brief", demonstrations=())
            class Creator:
                calls = 0
                def call(self, *_args):
                    self.calls += 1
                    return SimpleNamespace(text=json.dumps({"skill_md": "instructions"}))
            creator = Creator(); destination = root / "packages" / "v0"
            first = create_package(creator, "e", family, 0, "skill", destination)
            second = create_package(creator, "e", family, 0, "skill", destination)
            self.assertEqual(first, second)
            self.assertEqual(creator.calls, 1)

    def test_clustered_bootstrap_counts_tasks_not_replicas_as_independent(self):
        result = clustered_paired_bootstrap({"task-a": [(1, 0), (1, 0)], "task-b": [(0, 1), (0, 1)]}, 7, samples=100)
        self.assertEqual(result["n_tasks"], 2)
        self.assertEqual(result["n_cells"], 4)
        self.assertEqual(result["mean_delta"], 0)

    def test_development_cases_have_separate_resumable_episodes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); config, store = self.make(root); package = root / "package"; package.mkdir()
            paths = []
            for task_id in ("d1", "d2"):
                path = root / f"{task_id}.json"; path.write_text(task_id); paths.append(SimpleNamespace(id=task_id, public_input=path, family="f"))
            class Agent:
                calls = []
                def run(self, episode, *_args): self.calls.append(episode); return "answer"
            agent = Agent()
            with patch("actionbench.commands.grade", return_value={"primary": 1}):
                first = _validate_on_development(config, store, "f", 0, 0, paths, agent, "skill", package)
                second = _validate_on_development(config, store, "f", 0, 0, paths, agent, "skill", package)
            self.assertEqual(len(agent.calls), 2)
            self.assertEqual([item["primary"] for item in first], [1, 1])
            self.assertEqual(second, first)
            self.assertEqual(len({row["episode_id"] for row in store.resumable_episodes(config.campaign)}), 0)

    def test_pending_work_is_not_counted_as_a_zero_quality_result(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            store.create_episode("done", config.campaign, "task", "f", "action", 0); store.set_episode("done", "completed", retryable=False); store.save_evaluation("done", "f", {"primary": 1})
            store.create_episode("waiting", config.campaign, "task", "f", "skill", 0)
            report = build_report(config, store)
            self.assertEqual(report["groups"]["f:action"]["primary_on_terminal_episodes"], 1)
            self.assertIsNone(report["groups"]["f:skill"]["primary_on_terminal_episodes"])


if __name__ == "__main__": unittest.main()
