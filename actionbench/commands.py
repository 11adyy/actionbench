from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path

from .agent import AgentRunner
from .broker import Broker
from .errors import ActionBenchError, UnknownProviderOutcome
from .grader import grade
from .manifest import Manifest, load_manifest, verify_data
from .runner import ActionRunner
from .skill_creator import create_package


def dispatch(args, config, store) -> int:
    if args.command == "live-check":
        episode_id = f"{config.campaign}:live:{uuid.uuid4()}"
        store.create_episode(episode_id, config.campaign, episode_id, "integration", "plain", 0)
        result = Broker(config, store).call(episode_id, "healthcheck", "Reply with exactly OK.", "Health check.", 16)
        if result.text.strip() != "OK": raise ActionBenchError(f"Unexpected provider response: {result.text!r}")
        print(json.dumps({"provider_request_id": result.provider_request_id, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens, "actual_usd": result.actual_usd}, indent=2)); return 0
    if args.command == "report":
        from .report import build_report
        output = build_report(config, store)
        if args.out: Path(args.out).write_text(json.dumps(output, indent=2))
        print(json.dumps(output, indent=2)); return 0
    if args.command == "freeze":
        if store.resumable_episodes(config.campaign): raise ActionBenchError("Cannot freeze while work is resumable")
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
    if args.command == "create-skills":
        failures = verify_data(manifest)
        if failures: raise ActionBenchError("\n".join(failures))
        _create_packages(config, store, manifest)
        print(json.dumps({"status": "created", "families": len(manifest.families), "replicas": config.replicas}, indent=2)); return 0
    if args.command in {"run", "resume"}:
        failures = verify_data(manifest)
        if failures: raise ActionBenchError("\n".join(failures))
        _plan_test_episodes(config, store, manifest)
        _execute(config, store, manifest)
        print(json.dumps(store.campaign_status(config.campaign), indent=2)); return 0
    raise ActionBenchError(f"Unsupported command: {args.command}")


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
            for kind in ("skill", "action"):
                if store.package(config.campaign, family.id, replica, kind): continue
                episode = _creation_episode(config, family.id, replica, kind)
                store.create_episode(episode, config.campaign, f"creation:{family.id}:{kind}:{replica}", family.id, kind, replica)
                store.set_episode(episode, "running")
                feedback: list[dict] = []
                try:
                    final_path = None; final_hash = None
                    base_skill_md = None
                    if kind == "action":
                        paired = store.package(config.campaign, family.id, replica, "skill")
                        if not paired: raise ActionBenchError("Create the paired conventional skill before its action package")
                        base_skill_md = (Path(paired["path"]) / "SKILL.md").read_text()
                    for revision in range(3):
                        target = base / family.id / str(replica) / kind / f"v{revision}"
                        package_hash = create_package(broker, episode, family, replica, kind, target, feedback, base_skill_md)
                        feedback = _validate_on_development(episode, dev_tasks, agent, kind, target)
                        final_path, final_hash = target, package_hash
                        if all(item["primary"] >= 1 for item in feedback): break
                    assert final_path and final_hash
                    store.save_package(config.campaign, family.id, replica, kind, final_hash, str(final_path), episode)
                    store.set_episode(episode, "completed")
                except UnknownProviderOutcome as exc:
                    store.set_episode(episode, "failed", error=str(exc), retryable=False); raise
                except Exception as exc:
                    store.set_episode(episode, "failed", error=str(exc), retryable=True); raise


def _validate_on_development(episode: str, tasks, agent: AgentRunner, kind: str, package: Path) -> list[dict]:
    feedback = []
    for task in tasks:
        answer = agent.run(episode, task.public_input.read_text(), kind, package if kind == "skill" else None, package if kind == "action" else None)
        score = grade(task, answer)
        feedback.append({"task_id": task.id, "primary": score["primary"], "details": score})
    return feedback


def _plan_test_episodes(config, store, manifest: Manifest) -> None:
    for task in manifest.test_tasks:
        for replica in range(config.replicas):
            conventional = store.package(config.campaign, task.family, replica, "skill")
            actions = store.package(config.campaign, task.family, replica, "action")
            if not conventional or not actions: raise ActionBenchError(f"Missing packages for {task.family} replica {replica}; run create-skills")
            packages = {"plain": None, "skill": conventional["package_hash"], "improvised": conventional["package_hash"], "action": actions["package_hash"]}
            for condition in config.conditions:
                episode_id = _id(config.campaign, "test", task.id, condition, replica, packages[condition] or "plain")
                store.create_episode(episode_id, config.campaign, task.id, task.family, condition, replica, packages[condition])


def _execute(config, store, manifest: Manifest) -> None:
    tasks = {task.id: task for task in manifest.test_tasks}; broker = Broker(config, store); tools = ActionRunner(config, broker); agent = AgentRunner(broker, tools)
    for row in store.resumable_episodes(config.campaign):
        task = tasks.get(row["task_id"])
        if not task: continue
        conventional = store.package(config.campaign, task.family, row["replica"], "skill")
        actions = store.package(config.campaign, task.family, row["replica"], "action")
        skill_dir = Path(conventional["path"]) if row["condition"] in {"skill", "improvised"} else None
        action_dir = Path(actions["path"]) if row["condition"] == "action" else None
        store.set_episode(row["episode_id"], "running")
        try:
            answer = agent.run(row["episode_id"], task.public_input.read_text(), row["condition"], skill_dir, action_dir)
            output_dir = config.artifact_root / "answers" / config.campaign; output_dir.mkdir(parents=True, exist_ok=True)
            answer_path = output_dir / f"{row['episode_id']}.txt"; answer_path.write_text(answer)
            score = grade(task, answer)
            store.save_evaluation(row["episode_id"], task.family, score)
            store.set_episode(row["episode_id"], "completed", final_artifact=str(answer_path), retryable=False)
        except UnknownProviderOutcome as exc:
            store.set_episode(row["episode_id"], "failed", error=str(exc), retryable=False)
        except Exception as exc:
            store.set_episode(row["episode_id"], "failed", error=str(exc), retryable=True)
