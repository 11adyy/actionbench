import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from actionbench.agent import AgentRunner
from actionbench.commands import _development_episode, _read_saved_answer, _validate_on_development, _verified_package
from actionbench.cli import campaign_lock
from actionbench.config import load_config
from actionbench.broker import Broker
from actionbench.errors import ActionBenchError, InfrastructureError, UnknownProviderOutcome
from actionbench.grader import grade
from actionbench.report import build_report
from actionbench.runner import ActionRunner
from actionbench.skill_creator import create_package
from actionbench.store import Store
from actionbench.statistics import crossed_paired_bootstrap


class CoreTests(unittest.TestCase):
    def make(self, root: Path, campaign="c"):
        raw = json.loads((Path(__file__).parents[1] / "config.example.json").read_text())
        raw.update({"campaign": campaign, "dataset_root": "data", "artifact_root": "artifacts"})
        path = root / "config.json"; path.write_text(json.dumps(raw))
        config = load_config(path); store = Store(config.db_path); store.ensure_campaign(config)
        self.addCleanup(store.close)
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

    def test_missing_provider_usage_blocks_instead_of_recording_zero_cost(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "plain", 0)
            broker = Broker(config, store)
            broker.client = type("Client", (), {"request": lambda self, _: {"id": "p", "output_text": "ok"}})()
            with self.assertRaises(UnknownProviderOutcome): broker.call("e", "k", "i", "x", 4)
            row = store.conn.execute("SELECT state,actual_usd FROM requests WHERE episode_id='e'").fetchone()
            self.assertEqual(row["state"], "unknown_outcome")
            self.assertIsNone(row["actual_usd"])

    def test_second_coordinator_cannot_take_campaign_lock(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ledger.sqlite3"
            with campaign_lock(path):
                with self.assertRaises(ActionBenchError):
                    with campaign_lock(path): pass

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

    def test_multiple_procedures_are_sorted_and_exposed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "SKILL.md").write_text("skill")
            for procedure_id in ("zeta", "alpha"):
                directory = root / "procedures" / procedure_id; directory.mkdir(parents=True)
                (directory / "procedure.json").write_text(json.dumps({"id": procedure_id, "description": procedure_id, "input_schema": {"type": "object"}, "command": ["python", "/action/main.py"]}))
            class BrokerCapture:
                config = SimpleNamespace(budget=SimpleNamespace(max_llm_calls=1, max_output_tokens=64))
                def call(self, *_args): self.context = json.loads(_args[3]); return SimpleNamespace(text='{"type":"final","answer":"ok"}')
            broker = BrokerCapture(); AgentRunner(broker, None).run("e", "task", "action", None, root)
            self.assertEqual([item["id"] for item in broker.context["procedures"]], ["alpha", "zeta"])

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
            self.assertEqual(observed["command"][1], "/workspace/program.py")
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

    def test_crossed_bootstrap_preserves_package_replica_variance(self):
        cells = {(f"task-{task}", replica): ((1, 0) if replica == 0 else (0, 1)) for task in range(20) for replica in range(2)}
        result = crossed_paired_bootstrap(cells, 7, samples=500)
        self.assertEqual(result["n_tasks"], 20)
        self.assertEqual(result["n_replicas"], 2)
        self.assertEqual(result["n_cells"], 40)
        self.assertEqual(result["mean_delta"], 0)
        self.assertLess(result["ci95"][0], 0)
        self.assertGreater(result["ci95"][1], 0)

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

    def test_saved_development_answer_is_graded_without_another_agent_call(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); config, store = self.make(root); public = root / "task.json"; public.write_text("task")
            episode = _development_episode(config, "f", "skill", 0, 0, "d")
            store.create_episode(episode, config.campaign, "creation-dev:f:skill:0:0:d", "f", "skill", 0)
            answer = config.artifact_root / "answers" / config.campaign / f"{episode}.txt"
            answer.parent.mkdir(parents=True); answer.write_text("checkpointed answer")
            store.save_answer(episode, str(answer))
            class Agent:
                def run(self, *_args): raise AssertionError("agent must not run again")
            with patch("actionbench.commands.grade", return_value={"primary": 1}) as grader:
                result = _validate_on_development(config, store, "f", 0, 0, [SimpleNamespace(id="d", public_input=public, family="f")], Agent(), "skill", root)
            self.assertEqual(result[0]["primary"], 1)
            self.assertEqual(grader.call_args.args[1], "checkpointed answer")

    def test_answer_checkpoint_detects_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); config, store = self.make(root)
            store.create_episode("e", config.campaign, "task", "f", "plain", 0)
            answer = root / "answer.txt"; answer.write_text("original")
            store.save_answer("e", str(answer)); answer.write_text("changed")
            with self.assertRaises(InfrastructureError): _read_saved_answer(store.episode("e"), answer)

    def test_package_hash_detects_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); package = root / "package"; package.mkdir()
            skill = package / "SKILL.md"; skill.write_text("original")
            from actionbench.skill_creator import _package_hash
            row = {"path": str(package), "package_hash": _package_hash(package)}
            self.assertEqual(_verified_package(row), package)
            skill.write_text("changed")
            with self.assertRaises(InfrastructureError): _verified_package(row)

    def test_pending_work_is_not_counted_as_a_zero_quality_result(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            store.create_episode("done", config.campaign, "task", "f", "action", 0); store.set_episode("done", "completed", retryable=False); store.save_evaluation("done", "f", {"primary": 1})
            store.create_episode("waiting", config.campaign, "task", "f", "skill", 0)
            report = build_report(config, store)
            self.assertEqual(report["groups"]["f:action"]["primary_on_terminal_episodes"], 1)
            self.assertIsNone(report["groups"]["f:skill"]["primary_on_terminal_episodes"])

    def test_retryable_infrastructure_failure_remains_resumable_after_two_attempts(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "task", "f", "action", 0)
            store.set_episode("e", "running"); store.set_episode("e", "failed", error="temporary", retryable=True)
            self.assertTrue(store.episode("e")["retryable"])
            store.set_episode("e", "running"); store.set_episode("e", "failed", error="temporary", retryable=True)
            self.assertTrue(store.episode("e")["retryable"])
            self.assertEqual([r["episode_id"] for r in store.resumable_episodes(config.campaign)], ["e"])

    def test_stalled_running_episode_remains_resumable_after_two_attempts(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "task", "f", "action", 0)
            store.set_episode("e", "running"); store.set_episode("e", "running")
            self.assertEqual([r["episode_id"] for r in store.resumable_episodes(config.campaign)], ["e"])
            row = store.episode("e")
            self.assertEqual(row["status"], "running")

    def test_protocol_failure_in_development_is_terminal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); config, store = self.make(root); source = root / "task.json"; source.write_text("task")
            class BrokenAgent:
                def run(self, *_args): raise ActionBenchError("invalid tool request")
            feedback = _validate_on_development(config, store, "f", 0, 0, [SimpleNamespace(id="d", public_input=source, family="f")], BrokenAgent(), "skill", root)
            self.assertEqual(feedback[0]["primary"], 0)
            episode = store.resumable_episodes(config.campaign)
            self.assertEqual(episode, [])

    def test_grader_has_a_writable_temporary_filesystem(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); public = root / "task.json"; public.write_text("{}")
            reference = root / "reference"; reference.mkdir()
            task = SimpleNamespace(public_input=public, reference_dir=reference, grader={"image": "grader:test", "command": ["grade"]})
            observed = {}
            def fake_run(command, **_kwargs): observed["command"] = command; return SimpleNamespace(returncode=0, stdout='{"primary": 1}', stderr="")
            with patch("actionbench.grader.shutil.which", return_value="docker"), patch("actionbench.grader.subprocess.run", side_effect=fake_run):
                self.assertEqual(grade(task, "answer")["primary"], 1)
            self.assertIn("/tmp:rw,nosuid,size=256m", observed["command"])

    def test_action_amortization_does_not_subtract_the_shared_skill_cost(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            def add_cost(episode, task_id, condition, amount):
                store.create_episode(episode, config.campaign, task_id, "f", condition, 0)
                store.set_episode(episode, "completed", retryable=False)
                store.reserve_request(f"request-{episode}", episode, "k", "h", {}, amount, 1)
                store.mark_submitted(f"request-{episode}")
                store.complete_request(f"request-{episode}", None, {}, {}, amount)
            add_cost("create-skill", "creation:f:skill:0", "skill", 5)
            add_cost("create-action", "creation:f:action:0", "action", 7)
            for condition in ("skill", "action"):
                add_cost(f"test-{condition}", "task", condition, 0)
                store.save_evaluation(f"test-{condition}", "f", {"primary": 1})
            comparison = build_report(config, store)["paired_comparisons"]["f:action_minus_skill"]
            self.assertEqual(comparison["amortization"]["mean_creation_delta_usd_per_replica"], 7)


if __name__ == "__main__": unittest.main()
