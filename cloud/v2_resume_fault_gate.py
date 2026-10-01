"""Hosted fault injection for a known-outcome interrupted test episode."""
from __future__ import annotations

from pathlib import Path

from actionbench.v2_cli import frozen_config, load, manifest_for
from actionbench.v2_runner import run_episode
from actionbench.v2_store import Ledger, digest


def main() -> None:
    config=load(Path("experiment-v2.json"))
    root=Path(config["artifact_root"])
    manifest=manifest_for(config)
    task=next(item for item in manifest["tasks"] if item["family"]=="file_exploration" and item["split"]=="development")
    replica=999  # No package exists; this gate tests recovery, not quality.
    episode=f"test:{task['id']}:{replica}:script_only:0.003"
    ledger=Ledger(root/"actionbench-v2.sqlite3",config["campaign"],frozen_config(config,manifest))
    try:
        previous=ledger.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        ledger.begin_episode(episode,task["id"],task["family"],replica,"script_only",0.003)
        ledger.set_episode(episode,"running")
        workspace=root/"workspaces"/digest(episode)
        workspace.mkdir(parents=True,exist_ok=False)
        (workspace/"interrupted-attempt.txt").write_text("preserve me\n")
        checkpoint=root/"checkpoints"/(digest(episode)+".sqlite3")
        checkpoint.parent.mkdir(parents=True,exist_ok=True)
        checkpoint.write_text("interrupted checkpoint\n")
        run_episode(config,ledger,None,task,replica,"script_only",0.003,root)
        archive=root/"interrupted"/digest(episode)/"0"
        if (archive/"workspace/interrupted-attempt.txt").read_text()!="preserve me\n":
            raise ValueError("Interrupted workspace was not preserved")
        if (archive/"checkpoint").read_text()!="interrupted checkpoint\n":
            raise ValueError("Interrupted checkpoint was not preserved")
        if ledger.episode(episode)["status"]!="failed" or ledger.episode(episode)["error"]!="Skill package unavailable":
            raise ValueError("Known-outcome episode did not reach its expected next terminal state")
        if ledger.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]!=previous:
            raise ValueError("Fault injection repeated a provider request")
        print("Known-outcome interrupted episode archived and resumed without a provider call")
    finally:ledger.close()


if __name__=="__main__":main()
