from __future__ import annotations

import hashlib
import gzip
import json
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .agent import AgentRunner
from .broker import Broker
from .contracts import decision_format
from .errors import ActionBenchError, BudgetExceeded, CampaignBudgetExceeded, ConfigurationError, InfrastructureError, UnknownProviderOutcome
from .grader import grade
from .manifest import Manifest, load_manifest, verify_data
from .runner import ActionRunner, ContainerRunner
from .skill_creator import create_package, _package_hash


def dispatch(args, config, store) -> int:
    if args.command == "live-check":
        episode_id = f"{config.campaign}:live:{uuid.uuid4()}"
        store.create_episode(episode_id, config.campaign, episode_id, "integration", "plain", 0)
        store.set_episode(episode_id, "running")
        try:
            result = Broker(config, store).call(episode_id, "healthcheck", "Reply with exactly OK.", "Health check.", 16)
            if result.text.strip() != "OK": raise ActionBenchError(f"Unexpected provider response: {result.text!r}")
            store.set_episode(episode_id, "completed", retryable=False)
        except Exception as exc:
            store.set_episode(episode_id, "failed", error=str(exc), retryable=False)
            raise
        print(json.dumps({"provider_request_id": result.provider_request_id, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens, "actual_usd": result.actual_usd}, indent=2)); return 0
    if args.command == "budget":
        value = store.raise_budget_ceiling(config, args.usd)
        print(json.dumps({"campaign": config.campaign, "ceiling_usd": value}, indent=2)); return 0
    if args.command == "resolve-request":
        try: response = json.loads(Path(args.response_file).read_text()) if args.response_file else None
        except (OSError, json.JSONDecodeError) as exc: raise ActionBenchError(f"Cannot read genuine provider response: {exc}") from exc
        broker = Broker(config, store)
        usage = broker._usage(response) if response is not None else None
        actual = broker._cost(usage) if usage is not None else None
        store.reconcile_request(config.campaign, args.request_id, evidence=args.evidence, response=response, usage=usage, actual_usd=actual)
        print(json.dumps({"request_id": args.request_id, "resolution": "response_recovered" if response is not None else "confirmed_not_executed"}, indent=2)); return 0
    if args.command == "plan-sample":
        from .design import plan_sample
        output = plan_sample(config, store, args.family, args.baseline, args.target_delta, args.target_half_width)
        if args.out: Path(args.out).write_text(json.dumps(output, indent=2))
        print(json.dumps(output, indent=2)); return 0
    if args.command == "report":
        from .report import build_report
        output = build_report(config, store)
        if args.out: Path(args.out).write_text(json.dumps(output, indent=2))
        print(json.dumps(output, indent=2)); return 0
    if args.command == "freeze":
        binding = store.study_binding(config.campaign)
        if not binding: raise ActionBenchError("Cannot freeze an unbound campaign")
        row = store.conn.execute("""SELECT COUNT(*) total, SUM(CASE WHEN (e.status='completed' AND ev.episode_id IS NOT NULL) OR (e.status='failed' AND e.retryable=0) THEN 1 ELSE 0 END) terminal
                                  FROM episodes e LEFT JOIN evaluations ev ON ev.episode_id=e.episode_id
                                  WHERE e.campaign=? AND e.family!='integration' AND e.task_id NOT LIKE 'creation%'""", (config.campaign,)).fetchone()
        if row["total"] != binding["planned_test_episodes"] or row["terminal"] != row["total"]:
            raise ActionBenchError("Cannot freeze until every planned test episode has a scored or terminal outcome")
        if store.conn.execute("SELECT 1 FROM episodes WHERE campaign=? AND status='blocked' LIMIT 1", (config.campaign,)).fetchone():
            raise ActionBenchError("Cannot freeze while provider outcomes need manual resolution")
        store.conn.execute("UPDATE campaigns SET status='frozen',frozen_at=datetime('now') WHERE campaign=?", (config.campaign,))
        print(json.dumps({"campaign": config.campaign, "status": "frozen"}, indent=2)); return 0
    if args.command == "images":
        if not shutil.which("docker"): raise ActionBenchError("Docker is required to build benchmark grader images")
        root = Path(__file__).parents[1] / "graders"
        for name, image in (("mbppplus", "actionbench-mbppplus:v1"), ("hotpotqa", "actionbench-hotpot:v1")):
            subprocess.run(["docker", "build", "-t", image, str(root / name)], check=True)
        print(json.dumps({"built": ["actionbench-mbppplus:v1", "actionbench-hotpot:v1"]}, indent=2)); return 0
    if args.command == "datasets" and not args.manifest:
        from .datasets import prepare_study
        output = prepare_study(config.dataset_root, Path(args.out or config.source_path.parent / "manifests" / "study.json"))
        print(json.dumps(output, indent=2)); return 0
    if not args.manifest: raise ActionBenchError(f"{args.command} requires --manifest")
    manifest = load_manifest(args.manifest, config.dataset_root)
    if args.command == "datasets":
        failures = verify_data(manifest)
        if failures: raise ActionBenchError("\n".join(failures))
        store.event(None, "datasets_verified", {"manifest_hash": manifest.fingerprint, "test_tasks": len(manifest.test_tasks), "development_tasks": len(manifest.development_tasks)})
        print(json.dumps({"manifest_hash": manifest.fingerprint, "test_tasks": len(manifest.test_tasks), "development_tasks": len(manifest.development_tasks)}, indent=2)); return 0
    if args.command == "smoke":
        failures = verify_data(manifest)
        if failures: raise ActionBenchError("\n".join(failures))
        harness_hash, image_hashes = _study_inputs(config, manifest)
        output = _smoke(manifest, config)
        output["program_runner"] = _container_smoke(config)
        store.record_gate(config.campaign, "grader_smoke", manifest.fingerprint, harness_hash, image_hashes, output)
        print(json.dumps(output, indent=2)); return 0
    if args.command == "integration-check":
        failures = verify_data(manifest)
        if failures: raise ActionBenchError("\n".join(failures))
        if config.provider.model == "SET_A_REAL_MODEL" or not all(value > 0 for value in (config.provider.input_usd_per_million, config.provider.output_usd_per_million)):
            raise ActionBenchError("Configure a real model and positive token prices before the integration check")
        harness_hash, image_hashes = _study_inputs(config, manifest)
        output = _integration_check(config, store)
        store.record_gate(config.campaign, "action_broker", manifest.fingerprint, harness_hash, image_hashes, output)
        print(json.dumps(output, indent=2)); return 0
    if args.command == "create-skills":
        failures = verify_data(manifest)
        if failures: raise ActionBenchError("\n".join(failures))
        _bind_study(config, store, manifest)
        _create_packages(config, store, manifest)
        outcomes = store.conn.execute("SELECT status,COUNT(*) n FROM episodes WHERE campaign=? AND task_id LIKE 'creation:%' GROUP BY status", (config.campaign,)).fetchall()
        print(json.dumps({"status": "creation_attempted", "packages": {row["status"]: row["n"] for row in outcomes}}, indent=2)); return 0
    if args.command == "canary":
        result = _canary(config, store, manifest)
        print(json.dumps(result, indent=2)); return 0
    if args.command in {"run", "resume"}:
        failures = verify_data(manifest)
        if failures: raise ActionBenchError("\n".join(failures))
        _bind_study(config, store, manifest)
        _plan_test_episodes(config, store, manifest)
        _execute(config, store, manifest)
        print(json.dumps(store.campaign_status(config.campaign), indent=2)); return 0
    raise ActionBenchError(f"Unsupported command: {args.command}")


def _bind_study(config, store, manifest: Manifest) -> None:
    if config.provider.model == "SET_A_REAL_MODEL" or not all(value > 0 for value in (config.provider.input_usd_per_million, config.provider.output_usd_per_million)):
        raise ActionBenchError("Set a real provider model and positive verified input/output token prices before starting the study")
    harness_hash, image_hashes = _study_inputs(config, manifest)
    fingerprint = (manifest.fingerprint, harness_hash, json.dumps(image_hashes, sort_keys=True))
    for kind in ("grader_smoke", "action_broker"):
        gate = store.gate(config.campaign, kind)
        if not gate or (gate["manifest_hash"], gate["harness_hash"], gate["image_hashes_json"]) != fingerprint:
            raise ActionBenchError(f"Required {kind} gate is missing or stale; run smoke and integration-check for this exact study")
    store.bind_study(config.campaign, manifest.fingerprint, harness_hash, image_hashes, len(manifest.test_tasks) * config.replicas * len(config.conditions))


def _study_inputs(config, manifest: Manifest) -> tuple[str, dict[str, str]]:
    if not shutil.which("docker"): raise InfrastructureError("Docker is required before binding an experiment")
    root = Path(__file__).parents[1]
    digest = hashlib.sha256()
    for source in sorted((*root.joinpath("actionbench").glob("*.py"), *root.joinpath("graders").rglob("*"))):
        if source.is_file() and not source.name.endswith(".pyc"):
            digest.update(source.relative_to(root).as_posix().encode()); digest.update(source.read_bytes())
    images = sorted({config.execution.docker_image, *(task.grader["image"] for task in manifest.tasks)})
    image_hashes = {}
    for name in images:
        result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", name], capture_output=True, text=True)
        if result.returncode: raise InfrastructureError(f"Missing required Docker image {name}; run images build and pull the execution image")
        image_hashes[name] = result.stdout.strip()
    return digest.hexdigest(), image_hashes


def _integration_check(config, store) -> dict:
    from . import action_sdk
    episode = f"{config.campaign}:integration:{uuid.uuid4().hex}"
    store.create_episode(episode, config.campaign, episode, "integration", "action", 0)
    root = config.artifact_root / "integration-probe" / episode
    root.mkdir(parents=True)
    (root / "main.py").write_text("import json,sys\nfrom action_sdk import ActionContext\nctx=ActionContext(json.loads(sys.stdin.readline())['input'])\nctx.emit({'text':ctx.call_llm('Reply with exactly OK.',instructions='Return exactly OK.',max_output_tokens=16)})\n")
    (root / "action_sdk.py").write_text(Path(action_sdk.__file__).read_text())
    (root / "procedure.json").write_text(json.dumps({"id": "broker-probe", "command": ["python", "/action/main.py"], "input_schema": {"type": "object"}}))
    store.set_episode(episode, "running")
    try:
        output = ActionRunner(config, Broker(config, store)).run(episode, root, "probe", {}, allow_llm=True)
        if output.get("text", "").strip() != "OK": raise ActionBenchError(f"Integration probe returned {output!r}")
        decision = Broker(config, store).call(episode, "structured-decision-probe",
            "Return the required decision object. Choose final and put exactly OK in answer; use null for the other fields.",
            "Complete the harness protocol check.", 128, response_format=decision_format())
        parsed = json.loads(decision.text)
        if parsed != {"type": "final", "answer": "OK", "code": None, "procedure_id": None, "input_json": None}:
            raise ActionBenchError(f"Structured decision probe returned {parsed!r}")
        store.set_episode(episode, "completed", retryable=False)
    except UnknownProviderOutcome as exc:
        store.set_episode(episode, "blocked", error=str(exc), retryable=False); raise
    except (ConfigurationError, InfrastructureError) as exc:
        store.set_episode(episode, "queued", error=str(exc)); raise
    except Exception as exc:
        store.set_episode(episode, "failed", error=str(exc), retryable=False); raise
    return {"episode_id": episode, "action_output": output, "structured_decision": parsed,
            "provider_requests": store.conn.execute("SELECT COUNT(*) n FROM requests WHERE episode_id=? AND state='completed'", (episode,)).fetchone()["n"]}


def _container_smoke(config) -> dict:
    """Exercise the production Docker runner, including colon paths and read-only action mounts."""
    with tempfile.TemporaryDirectory(prefix="actionbench-container-smoke-") as temp:
        root = Path(temp)
        root.chmod(0o755)
        action = root / "action:readonly"; action.mkdir()
        (action / "main.py").write_text(
            "import json,sys\nfrom pathlib import Path\n"
            "message=json.loads(sys.stdin.readline())\n"
            "try:\n Path('/action/write-denied').write_text('bad')\n readonly=False\n"
            "except OSError:\n readonly=True\n"
            "Path('/workspace/output.txt').write_text(message['input']['value'])\n"
            "print(json.dumps({'kind':'result','output':{'value':Path('/workspace/output.txt').read_text(),'readonly':readonly}}),flush=True)\n"
        )
        runner = ContainerRunner(config, None)
        result = runner.execute("smoke:colon", "docker-real", root / "workspace:colon", ["python", "/action/main.py"],
                                {"value": "round-trip"}, action_dir=action)
        if result != {"value": "round-trip", "readonly": True}:
            raise InfrastructureError(f"Production container runner failed its real round trip: {result}")
        return {"passed": True, "colon_path": True, "readonly_action": True, "jsonl_round_trip": True}


def _sample_procedure_input(schema: dict, public: dict) -> dict:
    """Construct a development-only integration input; never use a test answer."""
    result = {}
    prompt = public.get("prompt") or public.get("question") or json.dumps(public, sort_keys=True)
    for key, shape in schema.get("properties", {}).items():
        if key in public:
            result[key] = public[key]
        elif key in {"task", "problem", "text", "task_text", "problem_text", "prompt"}:
            result[key] = prompt
        elif key == "question":
            result[key] = public.get("question", prompt)
        elif key == "context":
            result[key] = public.get("context", [])
        elif "default" in shape:
            result[key] = shape["default"]
        else:
            declared_type = shape.get("type")
            if isinstance(declared_type, list):
                declared_type = next((item for item in declared_type if item != "null"), "string")
            result[key] = {"string": "development probe", "integer": 0, "number": 0,
                           "boolean": False, "array": [], "object": {}}.get(declared_type, "development probe")
    try:
        Draft202012Validator(schema).validate(result)
    except ValidationError as exc:
        raise ActionBenchError(f"Canary input does not satisfy generated schema: {exc.message}") from exc
    return result


def _canary(config, store, manifest) -> dict:
    if config.replicas != 1:
        raise ActionBenchError("Canary requires a frozen configuration with exactly one package replica")
    outcomes = []
    for family in manifest.families:
        task = next((item for item in family.tasks if item.split == "development"), None)
        if not task:
            raise ActionBenchError(f"Canary needs development tasks for {family.id}")
        public = json.loads(task.public_input.read_text())
        for kind in ("skill", "skill_script", "action"):
            package = store.package(config.campaign, family.id, 0, kind)
            if not package:
                raise ActionBenchError(f"Canary has no generated {kind} package for {family.id}")
            path = _verified_package(package)
            paired = store.package(config.campaign, family.id, 0, "skill")
            if (path / "SKILL.md").read_bytes() != (_verified_package(paired) / "SKILL.md").read_bytes():
                raise ActionBenchError(f"Canary {kind} did not preserve the paired skill")
            if kind == "skill":
                outcomes.append({"family": family.id, "kind": kind, "package_hash": package["package_hash"], "paired_skill": True})
                continue
            procedure_file = next(iter(sorted(path.glob("procedures/*/procedure.json"))), None)
            if not procedure_file:
                raise ActionBenchError(f"Canary {kind} exposes no procedure")
            procedure = json.loads(procedure_file.read_text())
            input_data = _sample_procedure_input(procedure["input_schema"], public)
            episode_id = f"{config.campaign}:canary:{family.id}:{kind}:{procedure['id']}"
            store.create_episode(episode_id, config.campaign, f"canary-direct:{family.id}:{kind}", "integration", kind, 0)
            existing = store.episode(episode_id)
            if existing["status"] != "completed":
                store.set_episode(episode_id, "running")
                try:
                    ActionRunner(config, Broker(config, store)).run(episode_id, procedure_file.parent, "direct", input_data, allow_llm=kind == "action")
                    store.set_episode(episode_id, "completed", retryable=False)
                except UnknownProviderOutcome as exc:
                    store.set_episode(episode_id, "blocked", error=str(exc), retryable=False, failure_kind="provider_outcome_unknown")
                    raise
                except Exception as exc:
                    store.set_episode(episode_id, "failed", error=str(exc), retryable=False, failure_kind="canary_execution_failed")
                    raise
            calls = store.conn.execute("SELECT COUNT(*) FROM requests WHERE episode_id=? AND state='completed'", (episode_id,)).fetchone()[0]
            if kind == "action" and calls < 1:
                raise ActionBenchError(f"Canary action {procedure['id']} made no brokered model call")
            outcomes.append({"family": family.id, "kind": kind, "package_hash": package["package_hash"],
                             "procedure_id": procedure["id"], "direct_model_calls": calls})
    return {"passed": True, "checks": outcomes}


def _smoke(manifest: Manifest, config) -> dict:
    root = config.dataset_root
    official = root / "raw" / "MbppPlus-v0.1.0.jsonl.gz"
    rows = {row["task_id"]: row for row in (json.loads(line) for line in gzip.decompress(official.read_bytes()).decode().splitlines())}
    outcome = {}
    for family in manifest.families:
        task = next((item for item in family.tasks if item.split == "development"), family.tasks[0])
        public = json.loads(task.public_input.read_text())
        if family.id == "mbppplus":
            correct = rows[public["evalplus_task_id"]]["canonical_solution"]
            incorrect = "\npass\n"
        elif family.id == "hotpotqa":
            gold = json.loads((task.reference_dir / "gold.json").read_text())
            correct = json.dumps({"answer": gold["answer"], "sp": gold["supporting_facts"]})
            incorrect = json.dumps({"answer": "__definitely_wrong__", "sp": []})
        else: continue
        positive, negative = grade(task, correct), grade(task, incorrect)
        if positive["primary"] < 1 or negative["primary"] >= 1:
            raise ActionBenchError(f"Grader smoke failed for {family.id}: correct={positive}, incorrect={negative}")
        outcome[family.id] = {"correct": positive, "incorrect": negative}
    if not outcome: raise ActionBenchError("No recognized benchmark families to smoke-test")
    return outcome


def _id(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:32]


def _read_saved_answer(row, expected_path: Path) -> str:
    if Path(row["final_artifact"]).resolve() != expected_path.resolve() or not expected_path.is_file():
        raise InfrastructureError(f"Saved answer path missing or changed: {expected_path}")
    digest = hashlib.sha256(expected_path.read_bytes()).hexdigest()
    if not row["final_artifact_sha256"] or digest != row["final_artifact_sha256"]:
        raise InfrastructureError(f"Saved answer hash mismatch: {expected_path}")
    return expected_path.read_text()


def _verified_package(row) -> Path:
    path = Path(row["path"])
    if not path.is_dir() or _package_hash(path) != row["package_hash"]:
        raise InfrastructureError(f"Package changed after creation: {path}")
    return path


def _creation_episode(config, family: str, replica: int, kind: str) -> str:
    return _id(config.campaign, "creation", family, replica, kind)


def _create_packages(config, store, manifest: Manifest) -> None:
    if not manifest.development_tasks: raise ActionBenchError("A real study needs held-out development tasks for package creation")
    broker = Broker(config, store); tools = ActionRunner(config, broker); agent = AgentRunner(broker, tools)
    base = config.artifact_root / "packages" / config.campaign
    for family in manifest.families:
        dev_tasks = [task for task in family.tasks if task.split == "development"]
        for replica in range(config.replicas):
            for kind in ("skill", "skill_script", "action"):
                previous = store.package(config.campaign, family.id, replica, kind)
                if previous:
                    _verified_package(previous)
                    continue
                episode = _creation_episode(config, family.id, replica, kind)
                store.create_episode(episode, config.campaign, f"creation:{family.id}:{kind}:{replica}", family.id, kind, replica)
                prior = store.episode(episode)
                if prior["status"] == "blocked": raise UnknownProviderOutcome(f"Creation episode {episode} needs provider-outcome review")
                if prior["status"] == "failed" and not prior["retryable"]:
                    continue
                store.set_episode(episode, "running")
                started = time.monotonic()
                feedback: list[dict] = []
                try:
                    final_path = None; final_hash = None; best_score = -1.0; last_path = None
                    base_skill_md = None
                    if kind in {"skill_script", "action"}:
                        paired = store.package(config.campaign, family.id, replica, "skill")
                        if not paired: raise ActionBenchError("Create the paired conventional skill before its action package")
                        base_skill_md = (_verified_package(paired) / "SKILL.md").read_text()
                    for revision in range(3):
                        target = base / family.id / str(replica) / kind / f"v{revision}"
                        try:
                            package_hash = create_package(broker, episode, family, replica, kind, target, feedback, base_skill_md, revision=revision, previous_package=last_path)
                        except (CampaignBudgetExceeded, ConfigurationError, InfrastructureError, UnknownProviderOutcome): raise
                        except ActionBenchError as exc:
                            feedback = [{"generation_error": str(exc)}]
                            store.event(episode, "package_revision_rejected", {"kind": kind, "revision": revision, "error": str(exc)[:1000]})
                            continue
                        store.event(episode, "package_revision_created", {"kind": kind, "revision": revision, "hash": package_hash})
                        feedback = _validate_on_development(config, store, family.id, replica, revision, dev_tasks, agent, kind, target)
                        last_path = target
                        candidate_score = sum(item["primary"] for item in feedback) / len(feedback)
                        if candidate_score > best_score:
                            final_path, final_hash, best_score = target, package_hash, candidate_score
                        if all(item["primary"] >= 1 for item in feedback): break
                    if not final_path or not final_hash:
                        cause = feedback[0].get("generation_error", "no structurally valid revision") if feedback else "no structurally valid revision"
                        raise ActionBenchError(f"No valid {kind} package was produced for {family.id} replica {replica}: {cause}")
                    store.save_package(config.campaign, family.id, replica, kind, final_hash, str(final_path), episode)
                    store.set_episode(episode, "completed")
                except UnknownProviderOutcome as exc:
                    store.set_episode(episode, "blocked", error=str(exc), retryable=False); raise
                except (ConfigurationError, InfrastructureError, CampaignBudgetExceeded) as exc:
                    store.set_episode(episode, "queued", error=str(exc)); raise
                except ActionBenchError as exc:
                    store.set_episode(episode, "failed", error=str(exc), retryable=False, failure_kind="package_creation_failed")
                    continue
                except Exception as exc:
                    store.set_episode(episode, "failed", error=str(exc), retryable=True); raise
                finally:
                    store.add_episode_duration(episode, time.monotonic() - started)


def _development_episode(config, family: str, kind: str, replica: int, revision: int, task_id: str) -> str:
    return _id(config.campaign, "creation-development", family, kind, replica, revision, task_id)


def _validate_on_development(config, store, family: str, replica: int, revision: int, tasks, agent: AgentRunner, kind: str, package: Path) -> list[dict]:
    """Evaluate each package revision in its own durable, budgeted episode."""
    feedback = []
    for task in tasks:
        episode = _development_episode(config, family, kind, replica, revision, task.id)
        store.create_episode(episode, config.campaign, f"creation-dev:{family}:{kind}:{replica}:{revision}:{task.id}", family, kind, replica)
        existing = store.episode(episode)
        saved = store.evaluation(episode)
        if existing["status"] == "completed" and saved:
            score = json.loads(saved["score_json"])
            feedback.append({"task_id": task.id, "primary": score["primary"], "details": score})
            continue
        if existing["status"] == "failed" and not existing["retryable"]:
            feedback.append({"task_id": task.id, "primary": 0.0, "error": existing["error"] or "Agent failed"})
            continue
        if existing["status"] == "blocked":
            raise UnknownProviderOutcome(f"Development evaluation {episode} has an unresolved provider outcome")
        store.set_episode(episode, "running")
        started = time.monotonic()
        try:
            answer_path = config.artifact_root / "answers" / config.campaign / f"{episode}.txt"
            if existing["final_artifact"]:
                answer = _read_saved_answer(existing, answer_path)
            else:
                answer = agent.run(episode, task.public_input.read_text(), kind, package if kind in {"skill", "skill_script"} else None, package if kind == "action" else None)
                answer_path.parent.mkdir(parents=True, exist_ok=True)
                answer_path.write_text(answer); store.save_answer(episode, str(answer_path))
            score = grade(task, answer)
            store.save_evaluation(episode, task.family, score)
            store.set_episode(episode, "completed", retryable=False)
        except UnknownProviderOutcome as exc:
            store.set_episode(episode, "blocked", error=str(exc), retryable=False)
            raise
        except CampaignBudgetExceeded as exc:
            store.set_episode(episode, "queued", error=str(exc))
            raise
        except (ConfigurationError, InfrastructureError) as exc:
            store.set_episode(episode, "queued", error=str(exc))
            raise
        except ActionBenchError as exc:
            store.set_episode(episode, "failed", error=str(exc), retryable=False, failure_kind="agent_error")
            feedback.append({"task_id": task.id, "primary": 0.0, "error": str(exc)})
            continue
        except Exception as exc:
            store.set_episode(episode, "failed", error=str(exc), retryable=True)
            raise
        finally:
            store.add_episode_duration(episode, time.monotonic() - started)
        feedback.append({"task_id": task.id, "primary": score["primary"], "details": score})
    return feedback


def _plan_test_episodes(config, store, manifest: Manifest) -> None:
    for task in manifest.test_tasks:
        for replica in range(config.replicas):
            conventional = store.package(config.campaign, task.family, replica, "skill")
            scripts = store.package(config.campaign, task.family, replica, "skill_script")
            actions = store.package(config.campaign, task.family, replica, "action")
            for kind, package in (("skill", conventional), ("skill_script", scripts), ("action", actions)):
                if package: _verified_package(package)
                else:
                    creation = store.episode(_creation_episode(config, task.family, replica, kind))
                    if not creation or creation["status"] != "failed" or creation["retryable"]:
                        raise ActionBenchError(f"Creation of {kind} for {task.family} replica {replica} is incomplete; run create-skills")
            packages = {"plain": None, "skill": conventional["package_hash"] if conventional else None, "skill_script": scripts["package_hash"] if scripts else None, "improvised": conventional["package_hash"] if conventional else None, "action": actions["package_hash"] if actions else None}
            for condition in config.conditions:
                missing = condition != "plain" and packages[condition] is None
                package_hash = packages[condition] or (f"creation-failed:{condition}" if missing else None)
                episode_id = _id(config.campaign, "test", task.id, condition, replica, package_hash or "plain")
                store.create_episode(episode_id, config.campaign, task.id, task.family, condition, replica, package_hash)
                if missing and store.episode(episode_id)["status"] == "queued":
                    store.set_episode(episode_id, "failed", error=f"Required {condition} package could not be created", retryable=False, failure_kind="package_unavailable")


def _execute(config, store, manifest: Manifest) -> None:
    tasks = {task.id: task for task in manifest.test_tasks}; broker = Broker(config, store); tools = ActionRunner(config, broker); agent = AgentRunner(broker, tools)
    rows = store.resumable_episodes(config.campaign)
    rows = sorted(rows, key=lambda row: hashlib.sha256(f"{config.campaign}|{row['task_id']}|{row['replica']}|{row['condition']}".encode()).digest())
    for row in rows:
        task = tasks.get(row["task_id"])
        if not task: continue
        conventional = store.package(config.campaign, task.family, row["replica"], "skill")
        scripts = store.package(config.campaign, task.family, row["replica"], "skill_script")
        actions = store.package(config.campaign, task.family, row["replica"], "action")
        skill_dir = _verified_package(conventional) if row["condition"] in {"skill", "improvised"} else (_verified_package(scripts) if row["condition"] == "skill_script" else None)
        action_dir = _verified_package(actions) if row["condition"] == "action" else None
        store.set_episode(row["episode_id"], "running")
        started = time.monotonic()
        try:
            output_dir = config.artifact_root / "answers" / config.campaign; output_dir.mkdir(parents=True, exist_ok=True)
            answer_path = output_dir / f"{row['episode_id']}.txt"
            if row["final_artifact"]:
                answer = _read_saved_answer(row, answer_path)
            else:
                answer = agent.run(row["episode_id"], task.public_input.read_text(), row["condition"], skill_dir, action_dir)
                answer_path.write_text(answer); store.save_answer(row["episode_id"], str(answer_path))
            score = grade(task, answer)
            store.save_evaluation(row["episode_id"], task.family, score)
            store.set_episode(row["episode_id"], "completed", final_artifact=str(answer_path), retryable=False)
        except UnknownProviderOutcome as exc:
            store.set_episode(row["episode_id"], "blocked", error=str(exc), retryable=False)
        except CampaignBudgetExceeded as exc:
            store.set_episode(row["episode_id"], "queued", error=str(exc)); break
        except (ConfigurationError, InfrastructureError) as exc:
            store.set_episode(row["episode_id"], "queued", error=str(exc)); raise
        except BudgetExceeded as exc:
            store.set_episode(row["episode_id"], "failed", error=str(exc), retryable=False, failure_kind="episode_budget_exhausted")
        except ActionBenchError as exc:
            store.set_episode(row["episode_id"], "failed", error=str(exc), retryable=False, failure_kind="agent_error")
        except Exception as exc:
            store.set_episode(row["episode_id"], "failed", error=str(exc), retryable=True)
        finally:
            store.add_episode_duration(row["episode_id"], time.monotonic() - started)
