from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from .agent import AgentRunner
from .broker import Broker
from .errors import ActionBenchError
from .grader import grade
from .manifest import load_manifest, verify_data
from .runner import ActionRunner
from .skill_creator import create_skill


def dispatch(args, config, store) -> int:
    if args.command == "live-check":
        episode_id = f"live-check-{uuid.uuid4()}"
        store.create_episode(episode_id, config.campaign, episode_id, "integration", "plain", 0)
        result = Broker(config, store).call(episode_id, "healthcheck", "Reply with exactly OK.", "Health check.", 16)
        if result.text.strip() != "OK":
            raise ActionBenchError(f"Unexpected provider response: {result.text!r}")
        print(json.dumps({"provider_request_id": result.provider_request_id, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens, "actual_usd": result.actual_usd}, indent=2))
        return 0
    if not args.manifest:
        raise ActionBenchError(f"{args.command} requires --manifest")
    manifest = load_manifest(args.manifest, config.dataset_root)
    if args.command == "datasets":
        failures = verify_data(manifest)
        if failures: raise ActionBenchError("\n".join(failures))
        store.event(None, "datasets_verified", {"manifest": str(manifest.path), "hash": manifest.fingerprint})
        print(json.dumps({"manifest_hash": manifest.fingerprint, "tasks": len(manifest.tasks), "families": len(manifest.families)}, indent=2)); return 0
    if args.command == "create-skills":
        failures = verify_data(manifest)
        if failures: raise ActionBenchError("\n".join(failures))
        broker = Broker(config, store); root = config.artifact_root / "skills" / config.campaign
        for family in manifest.families:
            for replica in range(config.replicas):
                if store.skill_package(config.campaign, family.id, replica): continue
                episode = f"creation-{family.id}-{replica}"
                store.create_episode(episode, config.campaign, episode, family.id, "action", replica)
                store.set_episode(episode, "running")
                target = root / family.id / str(replica)
                try:
                    package_hash = create_skill(broker, episode, family, replica, target)
                    store.save_skill_package(config.campaign, family.id, replica, package_hash, str(target))
                    store.set_episode(episode, "completed")
                except Exception as exc:
                    store.set_episode(episode, "failed", error=str(exc)); raise
        print(json.dumps({"created_or_existing": config.replicas * len(manifest.families)}, indent=2)); return 0
    if args.command in {"run", "resume"}:
        failures = verify_data(manifest)
        if failures: raise ActionBenchError("\n".join(failures))
        _plan(config, store, manifest)
        _execute(config, store, manifest)
        print(json.dumps(store.campaign_status(config.campaign), indent=2)); return 0
    if args.command == "freeze":
        if store.pending_episodes(config.campaign): raise ActionBenchError("Cannot freeze while episodes are queued or running")
        store.conn.execute("UPDATE campaigns SET status='frozen', frozen_at=datetime('now') WHERE campaign=?", (config.campaign,))
        print(json.dumps({"campaign": config.campaign, "status": "frozen"}, indent=2)); return 0
    if args.command == "report":
        from .report import build_report
        output = build_report(config, store)
        if args.out: Path(args.out).write_text(json.dumps(output, indent=2))
        print(json.dumps(output, indent=2)); return 0
    raise ActionBenchError(f"Command '{args.command}' is not implemented in this repository revision")


def _episode_id(config, task_id: str, condition: str, replica: int, skill_hash: str | None) -> str:
    basis = f"{config.campaign}|{task_id}|{condition}|{replica}|{skill_hash or ''}".encode()
    return hashlib.sha256(basis).hexdigest()[:24]


def _plan(config, store, manifest) -> None:
    for task in manifest.tasks:
        for replica in range(config.replicas):
            package = store.skill_package(config.campaign, task.family, replica)
            if not package: raise ActionBenchError(f"Missing generated skill for {task.family} replica {replica}; run create-skills first")
            for condition in config.conditions:
                skill_hash = package["package_hash"] if condition in {"skill", "improvised", "action"} else None
                episode_id = _episode_id(config, task.id, condition, replica, skill_hash)
                store.create_episode(episode_id, config.campaign, task.id, task.family, condition, replica, skill_hash)


def _execute(config, store, manifest) -> None:
    task_map = {task.id: task for task in manifest.tasks}; broker = Broker(config, store); action_runner = ActionRunner(config, broker); agent = AgentRunner(broker, action_runner)
    for row in store.pending_episodes(config.campaign):
        if row["status"] == "running":
            store.set_episode(row["episode_id"], "queued", error="Recovered after interrupted coordinator")
        task = task_map.get(row["task_id"])
        if not task: continue
        package = store.skill_package(config.campaign, task.family, row["replica"])
        skill_dir = Path(package["path"]) if row["condition"] != "plain" else None
        store.set_episode(row["episode_id"], "running")
        try:
            answer = agent.run(row["episode_id"], task.public_input.read_text(), row["condition"], skill_dir)
            out_dir = config.artifact_root / "answers" / config.campaign; out_dir.mkdir(parents=True, exist_ok=True)
            answer_path = out_dir / f"{row['episode_id']}.txt"; answer_path.write_text(answer)
            score = grade(task, answer)
            store.save_evaluation(row["episode_id"], task.family, score)
            store.set_episode(row["episode_id"], "completed", final_artifact=str(answer_path))
        except Exception as exc:
            store.set_episode(row["episode_id"], "failed", error=str(exc))
