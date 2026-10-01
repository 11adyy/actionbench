"""Repair only the secondary judge on a preserved v2 pilot state.

The treatment answers and original SQLite artifact are never edited. This
creates a clearly labelled derivative ledger with new metered judge calls.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from actionbench.v2_judge import judge_summaries
from actionbench.v2_store import Ledger


def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument("--state",type=Path,required=True)
    parser.add_argument("--data",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    state=args.state.resolve();data=args.data.resolve();out=args.out.resolve()
    if out.exists():raise ValueError("Posthoc output already exists; use a new directory")
    original=state/"artifacts-v2/actionbench-v2.sqlite3"
    manifest=json.loads((data/"manifest.json").read_text())
    config=json.loads((state/"experiment-v2.json").read_text())
    original_db=sqlite3.connect(f"file:{original}?mode=ro",uri=True)
    saved=json.loads(original_db.execute("SELECT config_json FROM campaign").fetchone()[0])
    manifest_hash=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
    if manifest_hash!=saved["manifest_sha256"]:raise ValueError("Reconstructed dataset manifest differs from the frozen pilot")
    if config["campaign"]!=saved["campaign"]:raise ValueError("Pilot identity differs")
    if not (state/".cloud-state/v2-source-sha").is_file():raise ValueError("Source identity missing")
    expected=original_db.execute("SELECT COUNT(*) FROM episodes WHERE condition IN ('script_only','script_llm') AND family IN ('file_summary','qmsum') AND task_id LIKE '%-test-%' AND status='completed'").fetchone()[0]
    if expected==0:raise ValueError("No frozen answers to judge")
    out.mkdir(parents=True)
    copy=out/"posthoc-judge.sqlite3"
    target=sqlite3.connect(copy)
    original_db.backup(target)
    target.close();original_db.close()
    config["dataset_root"]=str(data)
    config["artifact_root"]=str(out)
    ledger=Ledger(copy,config["campaign"],saved)
    try:
        result=judge_summaries(config,ledger,manifest,condition_name="judge_posthoc")
        paired=defaultdict(dict)
        for row in ledger.db.execute("""SELECT original.family,original.task_id,original.replica,original.budget_usd,
                    original.condition,review.score
                    FROM episodes review JOIN episodes original ON review.id='judge_posthoc:'||original.id
                    WHERE review.condition='judge_posthoc' AND review.status='completed'
                    AND original.condition IN ('script_only','script_llm')"""):
            key=(row[0],row[1],row[2],row[3]);paired[key][row[4]]=row[5]
        contrasts=[]
        for family in ("file_summary","qmsum"):
            values=[x["script_llm"]-x["script_only"] for key,x in paired.items()
                    if key[0]==family and set(x)=={"script_only","script_llm"}]
            contrasts.append({"family":family,"paired_judgments":len(values),
                              "mean_delta_llm_minus_only":sum(values)/len(values) if values else None})
        cost=ledger.db.execute("""SELECT COALESCE(SUM(COALESCE(c.actual_usd,c.reserved_usd)),0)
                FROM calls c JOIN episodes e ON e.id=c.episode_id WHERE e.condition='judge_posthoc'""").fetchone()[0]
        report={"source_campaign":config["campaign"],"source_sha256":(state/".cloud-state/v2-source-sha").read_text().strip(),
                "manifest_sha256":manifest_hash,"original_artifact_is_unchanged":True,
                "eligible_frozen_answers":expected,"judgments":result,"judge_cost_usd":cost,
                "paired_contrasts":contrasts,"interpretation":"Secondary model judgment only; blinded human review is still required."}
        (out/"posthoc-judge-report.json").write_text(json.dumps(report,indent=2)+"\n")
        print(json.dumps(report,indent=2))
        if result["completed"]!=expected or result["failed"] or result["blocked"]:
            raise ValueError("Secondary judging did not reach a complete terminal set; preserve derivative ledger")
    finally:ledger.close()


if __name__=="__main__":main()
