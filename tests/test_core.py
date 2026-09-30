import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from actionbench.agent import AgentRunner
from actionbench.commands import _bind_study, _creation_episode, _development_episode, _execute, _plan_test_episodes, _read_saved_answer, _validate_on_development, _verified_package
from actionbench.cli import campaign_lock
from actionbench.config import load_config
from actionbench.design import plan_sample
from actionbench.broker import Broker
from actionbench.errors import ActionBenchError, InfrastructureError, ProviderOutputError, ProviderRejectedError, ResumeConflict, UnknownProviderOutcome
from actionbench.grader import grade
from actionbench.report import build_report
from actionbench.runner import ActionRunner, ContainerRunner
from actionbench.skill_creator import create_package, _development_prompt_examples, _validate_procedure
from actionbench.store import Store
from actionbench.statistics import crossed_paired_bootstrap


class CoreTests(unittest.TestCase):
    def test_creator_uses_complete_deterministic_development_subset(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tasks = []
            for task_id, size in (("long", 12000), ("medium", 5100), ("short", 3900), ("test", 10)):
                path = root / f"{task_id}.json"
                path.write_text("x" * size)
                tasks.append(SimpleNamespace(id=task_id, split="test" if task_id == "test" else "development", public_input=path))
            family = SimpleNamespace(id="qa", tasks=tuple(tasks))
            selected = _development_prompt_examples(family)
            self.assertEqual([item["task_id"] for item in selected], ["short", "medium"])
            self.assertEqual([len(item["public_input"]) for item in selected], [3900, 5100])
            self.assertLessEqual(sum(len(item["public_input"].encode()) for item in selected), 10000)

    def make(self, root: Path, campaign="c"):
        raw = json.loads((Path(__file__).parents[1] / "config.example.json").read_text())
        raw.update({"campaign": campaign, "dataset_root": "data", "artifact_root": "artifacts"})
        path = root / "config.json"; path.write_text(json.dumps(raw))
        config = load_config(path); store = Store(config.db_path); store.ensure_campaign(config)
        self.addCleanup(store.close)
        return config, store

    def test_docker_bind_mount_accepts_episode_colons(self):
        with tempfile.TemporaryDirectory() as d:
            config, _ = self.make(Path(d))
            workspace = Path(d) / "pilot:integration:abc" / "attempt-1"
            action_dir = Path(d) / "pilot:action"
            with patch("actionbench.runner.shutil.which", return_value="/usr/bin/docker"):
                command = ContainerRunner(config, None)._docker(workspace, ["python", "main.py"], action_dir, "test")
            mounts = [command[i+1] for i, item in enumerate(command[:-1]) if item == "--mount"]
            self.assertEqual(len(mounts), 2)
            self.assertIn(f"source={workspace.resolve()},target=/workspace", mounts[0])
            self.assertIn(f"source={action_dir.resolve()},target=/action,readonly", mounts[1])
            self.assertNotIn("-v", command)

    def test_request_identity_includes_payload_hash(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "plain", 0)
            store.reserve_request("r-a", "e", "step", "hash-a", {"input": "A"}, .1, 10)
            self.assertIsNotNone(store.request_for("e", "step", "hash-a"))
            self.assertIsNone(store.request_for("e", "step", "hash-b"))

    def test_structured_schema_is_part_of_request_identity(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "plain", 0)
            broker = Broker(config, store)
            class Client:
                calls = 0
                def request(self, payload):
                    self.calls += 1
                    return {"id": f"p-{self.calls}", "output_text": "{}", "usage": {"input_tokens": 20, "output_tokens": 1}}
            broker.client = Client()
            one = {"type": "json_schema", "name": "v1", "strict": True, "schema": {"type": "object"}}
            two = {"type": "json_schema", "name": "v2", "strict": True, "schema": {"type": "object"}}
            broker.call("e", "same", "i", "x", 4, response_format=one)
            broker.call("e", "same", "i", "x", 4, response_format=one)
            broker.call("e", "same", "i", "x", 4, response_format=two)
            self.assertEqual(broker.client.calls, 2)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 2)

    def test_incomplete_provider_output_is_billed_once_and_not_consumed(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "plain", 0)
            replace = __import__("dataclasses").replace
            config = replace(config, provider=replace(config.provider, input_usd_per_million=1, output_usd_per_million=1))
            broker = Broker(config, store)
            class Client:
                calls = 0
                def request(self, payload):
                    self.calls += 1
                    return {"id": "p", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                            "output_text": "{", "usage": {"input_tokens": 20, "output_tokens": 4}}
            broker.client = Client()
            for _ in range(2):
                with self.assertRaises(ProviderOutputError): broker.call("e", "k", "i", "x", 4)
            self.assertEqual(broker.client.calls, 1)
            saved = store.conn.execute("SELECT state,response_status,incomplete_reason,output_validation FROM requests").fetchone()
            self.assertEqual(tuple(saved), ("completed", "incomplete", "max_output_tokens", "invalid"))
            self.assertGreater(store.campaign_spend(config.campaign), 0)

    def test_zero_usage_content_filter_is_known_and_not_retried(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "plain", 0)
            broker = Broker(config, store)
            raw = {"id": "filtered", "status": "incomplete", "incomplete_details": {"reason": "content_filter"},
                   "usage": {"input_tokens": 0, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}, "output_tokens": 0}}
            class Client:
                calls = 0
                def request(self, payload):
                    self.calls += 1
                    return raw
            broker.client = Client()
            for _ in range(2):
                with self.assertRaises(ProviderRejectedError): broker.call("e", "k", "i", "x", 4)
            self.assertEqual(broker.client.calls, 1)
            row = store.conn.execute("SELECT state,actual_usd,incomplete_reason,output_validation FROM requests").fetchone()
            self.assertEqual(tuple(row), ("completed", 0.0, "content_filter", "invalid"))

    def test_provider_refusal_is_terminal_and_not_retried(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "action", 0)
            raw = {"id": "refused", "status": "completed", "usage": {"input_tokens": 20, "output_tokens": 3},
                   "output": [{"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": "Cannot comply"}]}]}
            broker = Broker(config, store)
            class Client:
                calls = 0
                def request(self, payload):
                    self.calls += 1
                    return raw
            broker.client = Client()
            for _ in range(2):
                with self.assertRaises(ProviderRejectedError): broker.call("e", "k", "i", "x", 4)
            self.assertEqual(broker.client.calls, 1)
            row = store.conn.execute("SELECT state,output_validation FROM requests").fetchone()
            self.assertEqual(tuple(row), ("completed", "invalid"))

    def test_broker_uses_explicit_final_answer_not_commentary(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "action", 0)
            raw = {"id": "multiphase", "status": "completed", "usage": {"input_tokens": 30, "output_tokens": 8},
                   "output": [
                       {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "I will produce JSON."}]},
                       {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": '{"procedures":[]}'}]},
                   ]}
            broker = Broker(config, store)
            broker.client = type("Client", (), {"request": lambda self, payload: raw})()
            first = broker.call("e", "k", "i", "x", 16)
            second = broker.call("e", "k", "i", "x", 16)
            self.assertEqual(first.text, '{"procedures":[]}')
            self.assertEqual(second.text, first.text)
            self.assertEqual(json.loads(store.conn.execute("SELECT response_json FROM requests").fetchone()[0]), raw)

    def test_ambiguous_multiphase_output_is_rejected_after_billing(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "action", 0)
            raw = {"id": "ambiguous", "status": "completed", "usage": {"input_tokens": 30, "output_tokens": 8},
                   "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "first"}]},
                              {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "second"}]}]}
            broker = Broker(config, store)
            class Client:
                calls = 0
                def request(self, payload):
                    self.calls += 1
                    return raw
            broker.client = Client()
            for _ in range(2):
                with self.assertRaises(ProviderOutputError): broker.call("e", "k", "i", "x", 16)
            self.assertEqual(broker.client.calls, 1)
            self.assertEqual(store.conn.execute("SELECT state,output_validation FROM requests").fetchone()[:], ("completed", "invalid"))

    def test_received_response_with_inconsistent_usage_is_preserved_for_review(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "plain", 0)
            broker = Broker(config, store)
            raw = {"id": "provider-known", "status": "completed", "model": "m", "output_text": "OK",
                   "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 80}, "output_tokens": 2}}
            class Client:
                calls = 0
                def request(self, payload):
                    self.calls += 1
                    return raw
            broker.client = Client()
            for _ in range(2):
                with self.assertRaises(UnknownProviderOutcome): broker.call("e", "k", "i", "x", 4)
            self.assertEqual(broker.client.calls, 1)
            saved = store.conn.execute("SELECT state,provider_request_id,response_json FROM requests").fetchone()
            self.assertEqual((saved["state"], saved["provider_request_id"]), ("unknown_outcome", "provider-known"))
            self.assertEqual(json.loads(saved["response_json"]), raw)
            store.set_episode("e", "blocked", retryable=False)
            with self.assertRaises(ResumeConflict):
                store.reconcile_request(config.campaign, store.conn.execute("SELECT request_id FROM requests WHERE episode_id='e'").fetchone()[0],
                                        evidence="response was received", response=None)
            store.create_episode("e2", config.campaign, "t2", "f", "plain", 0)
            broker.client.request = lambda payload: ["unexpected top-level response"]
            with self.assertRaises(UnknownProviderOutcome): broker.call("e2", "k", "i", "x", 4)
            saved_list = store.conn.execute("SELECT response_json FROM requests WHERE episode_id='e2'").fetchone()[0]
            self.assertEqual(json.loads(saved_list), ["unexpected top-level response"])

    def test_known_policy_rejection_is_never_resent_on_resume(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "action", 0)
            broker = Broker(config, store)
            class Client:
                calls = 0
                def request(self, payload):
                    self.calls += 1
                    raise ProviderRejectedError("invalid_prompt")
            broker.client = Client()
            for _ in range(2):
                with self.assertRaises(ProviderRejectedError): broker.call("e", "k", "i", "x", 4)
            self.assertEqual(broker.client.calls, 1)
            self.assertEqual(store.conn.execute("SELECT state FROM requests").fetchone()[0], "policy_rejected")

    def test_generated_procedure_underscores_and_paired_skill(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); destination = root / "package"
            code = "import json,sys\nfrom action_sdk import ActionContext\nctx=ActionContext(json.loads(sys.stdin.readline())['input'])\ntext=ctx.call_llm('Check input', max_output_tokens=16)\nctx.emit({'text': text})\n"
            procedure = {"id": "extract_function_contract", "description": "Extract a contract",
                         "input_schema_json": '{"type":"object"}', "code": code}
            self.assertEqual(_validate_procedure(procedure, "action"), {"type": "object"})
            for bad in ("../escape", "bad/name", "", "_hidden"):
                with self.assertRaises(ActionBenchError): _validate_procedure({**procedure, "id": bad}, "action")
            class Creator:
                def call(self, *_args, **_kwargs):
                    return SimpleNamespace(text=json.dumps({"procedures": [procedure]}))
            family = SimpleNamespace(id="f", creator_brief="brief", demonstrations=(), tasks=())
            create_package(Creator(), "e", family, 0, "action", destination, base_skill_md="original skill")
            self.assertEqual((destination / "SKILL.md").read_text(), "original skill")
            self.assertEqual(json.loads((destination / "procedures" / procedure["id"] / "procedure.json").read_text())["command"], ["python", "/action/main.py"])

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

    def test_unclassified_terminal_failure_invalidates_report(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            store.create_episode("e", config.campaign, "task", "code", "action", 0)
            store.set_episode("e", "failed", error="Unexpected runtime error", retryable=False,
                              failure_kind="unclassified_agent_error")
            report = build_report(config, store)
            self.assertEqual(report["validation_status"], "failed")
            self.assertIn("technical_or_unclassified_episode_failures", report["validation_reasons"])

    def test_policy_rejection_is_reported_separately_from_method_failure(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            store.create_episode("e", config.campaign, "task", "f", "action", 0)
            store.set_episode("e", "failed", error="invalid_prompt", retryable=False, failure_kind="provider_rejected")
            report = build_report(config, store)
            self.assertIn("provider_policy_rejections_observed", report["validation_reasons"])

    def test_policy_event_in_scored_test_still_blocks_inference(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            store.create_episode("e", config.campaign, "task", "f", "action", 0)
            store.reserve_request("r", "e", "first", "hash", {}, 0.1, 100)
            store.mark_submitted("r")
            store.reject_request("r", "invalid_prompt", policy=True)
            store.save_evaluation("e", "f", {"primary": 1})
            store.set_episode("e", "completed", retryable=False)
            report = build_report(config, store)
            self.assertEqual(report["provider_policy_events_by_phase"]["test"], 1)
            self.assertIn("provider_content_filter_or_policy_rejection_in_test", report["validation_reasons"])

    def test_report_checks_action_adoption_for_each_family(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            for family in ("first", "second"):
                episode = f"{family}-action"
                store.create_episode(episode, config.campaign, f"task-{family}", family, "action", 0)
                store.save_evaluation(episode, family, {"primary": 0.5})
                store.set_episode(episode, "completed", retryable=False)
            store.create_action_run("run", "first-action", "step", "procedure", "input", "/tmp/workspace")
            store.set_action_run("run", "completed", output={"ok": True})
            store.bind_study(config.campaign, "manifest", "harness", {"image": "digest"}, 2)
            report = build_report(config, store)
            self.assertEqual(report["action_invocations_by_family"], {"first": 1})
            self.assertIn("no_action_invocation_observed:second", report["validation_reasons"])
            self.assertNotIn("no_action_invocation_observed:first", report["validation_reasons"])

    def test_policy_preparation_includes_paired_skill_and_development(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            for episode, task_id, condition, cost in (
                ("skill-create", "creation:f:skill:0", "skill", 0.02),
                ("skill-dev", "creation-dev:f:skill:0:0:d", "skill", 0.03),
                ("action-create", "creation:f:action:0", "action", 0.04),
                ("action-dev", "creation-dev:f:action:0:0:d", "action", 0.05),
            ):
                store.create_episode(episode, config.campaign, task_id, "f", condition, 0)
                store.reserve_request(episode + "-r", episode, "call", "hash", {}, cost, 100)
                store.mark_submitted(episode + "-r")
                store.complete_request(episode + "-r", "provider", {"status": "completed"},
                                       {"input_tokens": 1, "cached_input_tokens": 0, "cache_write_tokens": 0, "output_tokens": 1}, cost)
            for condition in ("plain", "skill", "improvised", "action"):
                episode = f"test-{condition}"
                store.create_episode(episode, config.campaign, "task", "f", condition, 0)
                store.save_evaluation(episode, "f", {"primary": 0.5})
                store.set_episode(episode, "completed", retryable=False)
            prep = build_report(config, store)["policy_preparation_usd_by_replica"]
            self.assertAlmostEqual(prep["f:plain:0"], 0)
            self.assertAlmostEqual(prep["f:skill:0"], 0.05)
            self.assertAlmostEqual(prep["f:improvised:0"], 0.05)
            self.assertAlmostEqual(prep["f:action:0"], 0.14)

    def test_report_pairs_action_against_skill_by_task_and_replica(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            for condition, score in (("skill", 0), ("action", 1)):
                episode = f"{condition}-e"; store.create_episode(episode, config.campaign, "task", "code", condition, 0, condition)
                store.set_episode(episode, "completed", retryable=False); store.save_evaluation(episode, "code", {"primary": score})
            comparison = build_report(config, store)["paired_comparisons"]["code:action_minus_skill"]
            self.assertEqual(comparison["quality"]["n"], 1)
            self.assertEqual(comparison["quality"]["mean_delta"], 1)

    def test_missing_action_package_does_not_become_a_cost_saving_claim(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            for condition in ("skill", "action"):
                episode = f"test-{condition}"
                store.create_episode(episode, config.campaign, "task", "f", condition, 0)
                store.set_episode(episode, "failed", error="Required action package could not be created", retryable=False,
                                  failure_kind="package_unavailable")
            store.bind_study(config.campaign, "manifest", "harness", {"image": "digest"}, 2)
            report = build_report(config, store)
            comparison = report["paired_comparisons"]["f:action_minus_skill"]
            self.assertEqual(report["scientific_status"], "diagnostic_only")
            self.assertFalse(comparison["interpretable"])
            self.assertIsNone(comparison["supports_total_cost_saving_with_quality_noninferiority"])
            self.assertIsNone(comparison["amortization"])
            store.conn.execute("UPDATE campaigns SET status='frozen' WHERE campaign=?", (config.campaign,))
            with self.assertRaises(ActionBenchError): plan_sample(config, store, "f", "skill", .1, .1)

    def test_valid_but_worse_action_is_preserved_as_negative_result(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            for replica in range(3):
                creation = f"create-{replica}"
                store.create_episode(creation, config.campaign, f"creation:f:action:{replica}", "f", "action", replica)
                store.set_episode(creation, "completed", retryable=False)
                store.save_package(config.campaign, "f", replica, "action", f"h-{replica}", f"/tmp/p-{replica}", creation)
            for task in range(10):
                for replica in range(3):
                    for condition, score in (("skill", 1), ("action", 0)):
                        episode = f"{condition}-{task}-{replica}"
                        store.create_episode(episode, config.campaign, f"task-{task}", "f", condition, replica)
                        store.save_evaluation(episode, "f", {"primary": score})
                        store.set_episode(episode, "completed", retryable=False)
            store.create_action_run("action-run", "action-0-0", "step", "procedure", "hash", "/tmp/workspace")
            store.set_action_run("action-run", "completed", output={"ok": True})
            store.bind_study(config.campaign, "manifest", "harness", {"image": "digest"}, 60)
            report = build_report(config, store)
            comparison = report["paired_comparisons"]["f:action_minus_skill"]
            self.assertEqual(report["scientific_status"], "exploratory")
            self.assertTrue(comparison["interpretable"])
            self.assertEqual(comparison["quality"]["mean_delta"], -1)
            self.assertFalse(comparison["supports_total_cost_saving_with_quality_noninferiority"])

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

    def test_unicode_input_uses_conservative_byte_reservation(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "plain", 0)
            broker = Broker(config, store)
            broker.client = type("Client", (), {"request": lambda self, _: {"id": "p", "output_text": "ok", "usage": {"input_tokens": 5, "output_tokens": 1}}})()
            broker.call("e", "k", "instructions", "你好世界", 4)
            reservation = store.conn.execute("SELECT reserved_input_tokens FROM requests WHERE episode_id='e'").fetchone()[0]
            self.assertGreaterEqual(reservation, len("instructions你好世界".encode()) + 1024)

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

    def test_cache_write_price_and_reasoning_are_frozen_in_real_request_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "t", "f", "plain", 0)
            replace = __import__("dataclasses").replace
            config = replace(config, provider=replace(config.provider, model="gpt-6-luna", input_usd_per_million=.1,
                cached_input_usd_per_million=.01, cache_write_usd_per_million=.125,
                output_usd_per_million=.5, reasoning_effort="none"))
            broker = Broker(config, store)
            class Client:
                def request(self, payload):
                    self.payload = payload
                    return {"id": "provider-1", "output_text": "OK", "usage": {
                        "input_tokens": 100, "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 30},
                        "output_tokens": 10}}
            broker.client = Client()
            result = broker.call("e", "k", "i", "x", 20)
            self.assertEqual(broker.client.payload["reasoning"], {"effort": "none"})
            self.assertAlmostEqual(result.actual_usd, (50 * .1 + 20 * .01 + 30 * .125 + 10 * .5) / 1_000_000)
            row = store.conn.execute("SELECT cache_write_tokens,request_json,actual_usd FROM requests WHERE episode_id='e'").fetchone()
            self.assertEqual(row["cache_write_tokens"], 30)
            self.assertEqual(json.loads(row["request_json"])["reasoning"], {"effort": "none"})
            self.assertEqual(row["actual_usd"], result.actual_usd)
            self.assertGreaterEqual(broker.estimate_usd(100, 10), (100 * .125 + 10 * .5) / 1_000_000)
            with self.assertRaises(UnknownProviderOutcome):
                broker._usage({"usage": {"input_tokens": 100, "output_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 30}}})

    def test_unknown_request_reconciles_with_audited_response(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "task", "f", "action", 0)
            store.reserve_request("r", "e", "step", "hash", {}, .1, 12)
            store.mark_submitted("r"); store.unknown_request("r", "connection lost")
            store.set_episode("e", "blocked", retryable=False)
            response = {"id": "provider-1", "output_text": "OK", "usage": {"input_tokens": 12, "output_tokens": 2}}
            usage = Broker(config, store)._usage(response)
            store.reconcile_request(config.campaign, "r", evidence="provider log entry 123", response=response, usage=usage, actual_usd=.03)
            self.assertEqual(store.request_by_id("r")["state"], "completed")
            self.assertEqual(store.episode("e")["status"], "queued")
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM events WHERE kind='manual_provider_reconciliation'").fetchone()[0], 1)

    def test_unknown_request_can_be_retried_only_after_confirmed_nonexecution(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d)); store.create_episode("e", config.campaign, "task", "f", "action", 0)
            store.reserve_request("r", "e", "step", "hash", {}, .1, 12)
            store.mark_submitted("r"); store.unknown_request("r", "connection lost")
            store.set_episode("e", "blocked", retryable=False)
            store.reconcile_request(config.campaign, "r", evidence="provider confirmed request absent", response=None)
            self.assertEqual(store.request_by_id("r")["state"], "reserved")
            self.assertEqual(store.episode("e")["status"], "queued")
            self.assertEqual(store.campaign_spend(config.campaign), .1)

    def test_reconciliation_unblocks_creation_parent_and_development_child(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            parent = _creation_episode(config, "f", 0, "action")
            child = _development_episode(config, "f", "action", 0, 0, "task")
            store.create_episode(parent, config.campaign, "creation:f:action:0", "f", "action", 0)
            store.create_episode(child, config.campaign, "creation-dev:f:action:0:0:task", "f", "action", 0)
            store.reserve_request("r", child, "step", "hash", {}, .1, 12)
            store.mark_submitted("r"); store.unknown_request("r", "connection lost")
            store.set_episode(parent, "blocked", retryable=False)
            store.set_episode(child, "blocked", retryable=False)
            store.reconcile_request(config.campaign, "r", evidence="provider confirmed nonexecution")
            self.assertEqual(store.episode(parent)["status"], "queued")
            self.assertEqual(store.episode(child)["status"], "queued")

    def test_study_requires_matching_recorded_external_gates(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            config = __import__("dataclasses").replace(config, provider=__import__("dataclasses").replace(config.provider, model="real-model", input_usd_per_million=1, output_usd_per_million=1))
            manifest = SimpleNamespace(fingerprint="manifest", test_tasks=[1], tasks=[])
            with patch("actionbench.commands._study_inputs", return_value=("harness", {"image": "digest"})):
                with self.assertRaisesRegex(ActionBenchError, "grader_smoke"):
                    _bind_study(config, store, manifest)
                store.record_gate(config.campaign, "grader_smoke", "manifest", "harness", {"image": "digest"}, {"passed": True})
                with self.assertRaisesRegex(ActionBenchError, "action_broker"):
                    _bind_study(config, store, manifest)
                store.record_gate(config.campaign, "action_broker", "manifest", "harness", {"image": "digest"}, {"passed": True})
                _bind_study(config, store, manifest)
            self.assertIsNotNone(store.study_binding(config.campaign))

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
                def call(self, *_args, **_kwargs):
                    self.context = json.loads(_args[3])
                    return SimpleNamespace(text='{"type":"final","answer":"done","code":null,"procedure_id":null,"input_json":null}')
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
                def call(self, *_args, **_kwargs): self.context = json.loads(_args[3]); return SimpleNamespace(text='{"type":"final","answer":"ok","code":null,"procedure_id":null,"input_json":null}')
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
                def call(self, *_args, **_kwargs):
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

    def test_saved_test_evaluation_is_reused_after_crash_before_completion(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); config, store = self.make(root)
            public = root / "task.json"; public.write_text('{}')
            task = SimpleNamespace(id="task", family="f", public_input=public)
            episode = "e"
            store.create_episode(episode, config.campaign, task.id, task.family, "plain", 0)
            answer = config.artifact_root / "answers" / config.campaign / f"{episode}.txt"
            answer.parent.mkdir(parents=True); answer.write_text("checkpointed answer")
            store.save_answer(episode, str(answer))
            store.save_evaluation(episode, "f", {"primary": 0.5})
            with patch("actionbench.commands.grade", side_effect=AssertionError("grader reran")), \
                 patch("actionbench.commands.AgentRunner.run", side_effect=AssertionError("agent reran")):
                _execute(config, store, SimpleNamespace(test_tasks=(task,)))
            self.assertEqual(store.episode(episode)["status"], "completed")
            self.assertEqual(json.loads(store.evaluation(episode)["score_json"]), {"primary": 0.5})

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

    def test_failed_package_creation_keeps_planned_test_denominator(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); config, store = self.make(root)
            config = __import__("dataclasses").replace(config, replicas=1)
            for kind in ("skill", "skill_script", "action"):
                episode = _creation_episode(config, "f", 0, kind)
                store.create_episode(episode, config.campaign, f"creation:f:{kind}:0", "f", kind, 0)
                store.set_episode(episode, "failed", error="invalid package", retryable=False)
            task = SimpleNamespace(id="held-out", family="f")
            _plan_test_episodes(config, store, SimpleNamespace(test_tasks=[task]))
            rows = store.conn.execute("SELECT condition,status FROM episodes WHERE task_id='held-out' ORDER BY condition").fetchall()
            self.assertEqual(len(rows), 5)
            self.assertEqual({row["condition"]: row["status"] for row in rows}, {"plain": "queued", "skill": "failed", "skill_script": "failed", "improvised": "failed", "action": "failed"})
            report = build_report(config, store)
            self.assertEqual(report["groups"]["f:action"]["primary_on_terminal_episodes"], 0)
            self.assertEqual(report["package_creation"]["action"]["failure_rate"], 1)

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
            replay = _validate_on_development(config, store, "f", 0, 0, [SimpleNamespace(id="d", public_input=source, family="f")], BrokenAgent(), "skill", root)
            self.assertEqual(replay, feedback)
            episode = store.resumable_episodes(config.campaign)
            self.assertEqual(episode, [])

    def test_agent_json_array_is_terminal_protocol_failure(self):
        class BrokerArray:
            config = SimpleNamespace(budget=SimpleNamespace(max_llm_calls=1, max_output_tokens=64))
            def call(self, *_args, **_kwargs): return SimpleNamespace(text="[]")
        with self.assertRaisesRegex(ActionBenchError, "required envelope"):
            AgentRunner(BrokerArray(), None).run("e", "task", "plain", None, None)

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
            store.save_package(config.campaign, "f", 0, "action", "action-hash", "/tmp/action", "create-action")
            for condition in ("skill", "action"):
                add_cost(f"test-{condition}", "task", condition, 0)
                store.save_evaluation(f"test-{condition}", "f", {"primary": 1})
            comparison = build_report(config, store)["paired_comparisons"]["f:action_minus_skill"]
            self.assertEqual(comparison["amortization"]["mean_creation_delta_usd_per_replica"], 7)

    def test_pilot_sample_planning_uses_paired_task_and_package_variance(self):
        with tempfile.TemporaryDirectory() as d:
            config, store = self.make(Path(d))
            for replica in range(2):
                episode = f"creation-{replica}"
                store.create_episode(episode, config.campaign, f"creation:f:action:{replica}", "f", "action", replica)
                store.set_episode(episode, "completed", retryable=False)
                store.save_package(config.campaign, "f", replica, "action", f"hash-{replica}", f"/tmp/action-{replica}", episode)
            for task in range(4):
                for replica in range(2):
                    for condition, score in (("skill", 0.2), ("action", 0.3 + task * .05 + replica * .1)):
                        episode = f"{task}-{replica}-{condition}"
                        store.create_episode(episode, config.campaign, f"task-{task}", "f", condition, replica)
                        store.save_evaluation(episode, "f", {"primary": score})
                        store.set_episode(episode, "completed", retryable=False)
            store.create_action_run("run-1", "0-0-action", "probe", "procedure", "input", "/tmp/workspace")
            store.set_action_run("run-1", "completed", output={"ok": True})
            store.bind_study(config.campaign, "manifest", "harness", {"image": "digest"}, 16)
            store.conn.execute("UPDATE campaigns SET status='frozen' WHERE campaign=?", (config.campaign,))
            plan = plan_sample(config, store, "f", "skill", .1, .1)
            self.assertEqual(plan["pilot_tasks"], 4)
            self.assertEqual(plan["pilot_package_replicas"], 2)
            self.assertGreater(plan["variance_components"]["task"], 0)
            self.assertGreater(plan["variance_components"]["package"], 0)
            self.assertTrue(all("approx_detection_probability" in item for item in plan["candidates"]))


if __name__ == "__main__": unittest.main()
