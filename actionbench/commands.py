from __future__ import annotations

import hashlib
import gzip
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .agent import AgentRunner
from .broker import Broker
from .errors import ActionBenchError, CampaignBudgetExceeded, InfrastructureError, UnknownProviderOutcome
from .grader import grade
from .manifest import Manifest, load_manifest, verify_data
from .runner import ActionRunner
from .skill_creator import create_package


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
        for name in ("mbppplus", "hotpotqa"):
            subprocess.run(["docker", "build", "-t", f"actionbench-{name}:v1", str(root / name)], check=True)
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
        output = _smoke(manifest, config)
        print(json.dumps(output, indent=2)); return 0
    if args.command == "create-skills":
        failures = verify_data(manifest)
        if failures: raise ActionBenchError("\n".join(failures))
        _bind_study(config, store, manifest)
        _create_packages(config, store, manifest)
        print(json.dumps({"status": "created", "families": len(manifest.families), "replicas": config.replicas}, indent=2)); return 0
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
    store.bind_study(config.campaign, manifest.fingerprint, digest.hexdigest(), image_hashes, len(manifest.test_tasks) * config.replicas * len(config.conditions))


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
                if store.package(config.campaign, family.id, replica, kind): continue
                episode = _creation_episode(config, family.id, replica, kind)
                store.create_episode(episode, config.campaign, f"creation:{family.id}:{kind}:{replica}", family.id, kind, replica)
                prior = store.episode(episode)
                if prior["status"] == "blocked": raise UnknownProviderOutcome(f"Creation episode {episode} needs provider-outcome review")
                if prior["status"] == "failed" and not prior["retryable"]:
                    raise ActionBenchError(f"Creation episode {episode} has a terminal failure; start a new campaign after fixing it")
                store.set_episode(episode, "running")
                started = time.monotonic()
                feedback: list[dict] = []
                try:
                    final_path = None; final_hash = None
                    base_skill_md = None
                    if kind in {"skill_script", "action"}:
                        paired = store.package(config.campaign, family.id, replica, "skill")
                        if not paired: raise ActionBenchError("Create the paired conventional skill before its action package")
                        base_skill_md = (Path(paired["path"]) / "SKILL.md").read_text()
                    for revision in range(3):
                        target = base / family.id / str(replica) / kind / f"v{revision}"
                        try:
                            package_hash = create_package(broker, episode, family, replica, kind, target, feedback, base_skill_md, revision=revision, previous_package=final_path)
                        except (CampaignBudgetExceeded, InfrastructureError, UnknownProviderOutcome): raise
                        except ActionBenchError as exc:
                            feedback = [{"generation_error": str(exc)}]
                            continue
                        feedback = _validate_on_development(config, store, family.id, replica, revision, dev_tasks, agent, kind, target)
                        final_path, final_hash = target, package_hash
                        if all(item["primary"] >= 1 for item in feedback): break
                    if not final_path or not final_hash: raise ActionBenchError(f"No valid {kind} package was produced for {family.id} replica {replica}")
                    store.save_package(config.campaign, family.id, replica, kind, final_hash, str(final_path), episode)
                    store.set_episode(episode, "completed")
                except UnknownProviderOutcome as exc:
                    store.set_episode(episode, "blocked", error=str(exc), retryable=False); raise
                except (InfrastructureError, CampaignBudgetExceeded) as exc:
                    store.set_episode(episode, "queued", error=str(exc)); raise
                except ActionBenchError as exc:
                    store.set_episode(episode, "failed", error=str(exc), retryable=False); raise
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
            raise ActionBenchError(f"Development evaluation {episode} needs manual resolution")
        store.set_episode(episode, "running")
        started = time.monotonic()
        try:
            answer_path = config.artifact_root / "answers" / config.campaign / f"{episode}.txt"
            if existing["final_artifact"]:
                if not answer_path.is_file(): raise InfrastructureError(f"Saved development answer missing: {answer_path}")
                answer = answer_path.read_text()
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
        except InfrastructureError as exc:
            store.set_episode(episode, "queued", error=str(exc))
            raise
        except ActionBenchError as exc:
            store.set_episode(episode, "failed", error=str(exc), retryable=False)
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
            if not conventional or not scripts or not actions: raise ActionBenchError(f"Missing packages for {task.family} replica {replica}; run create-skills")
            packages = {"plain": None, "skill": conventional["package_hash"], "skill_script": scripts["package_hash"], "improvised": conventional["package_hash"], "action": actions["package_hash"]}
            for condition in config.conditions:
                episode_id = _id(config.campaign, "test", task.id, condition, replica, packages[condition] or "plain")
                store.create_episode(episode_id, config.campaign, task.id, task.family, condition, replica, packages[condition])


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
        skill_dir = Path(conventional["path"]) if row["condition"] in {"skill", "improvised"} else (Path(scripts["path"]) if row["condition"] == "skill_script" else None)
        action_dir = Path(actions["path"]) if row["condition"] == "action" else None
        store.set_episode(row["episode_id"], "running")
        started = time.monotonic()
        try:
            output_dir = config.artifact_root / "answers" / config.campaign; output_dir.mkdir(parents=True, exist_ok=True)
            answer_path = output_dir / f"{row['episode_id']}.txt"
            if row["final_artifact"]:
                if not answer_path.is_file(): raise InfrastructureError(f"Saved answer missing: {answer_path}")
                answer = answer_path.read_text()
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
        except InfrastructureError as exc:
            store.set_episode(row["episode_id"], "queued", error=str(exc)); raise
        except ActionBenchError as exc:
            store.set_episode(row["episode_id"], "failed", error=str(exc), retryable=False)
        except Exception as exc:
            store.set_episode(row["episode_id"], "failed", error=str(exc), retryable=True)
        finally:
            store.add_episode_duration(row["episode_id"], time.monotonic() - started)
