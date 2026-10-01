"""Build a condition-blind human review packet from frozen completed pairs."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path


def _rank(value: str) -> str:
    return hashlib.sha256(("actionbench-v2-human-review-20261001|"+value).encode()).hexdigest()


def _source(family: str, folder: Path, reference: dict) -> str:
    if family=="file_summary":
        return "\n\n".join(f"PATH: {p.relative_to(folder).as_posix()}\n{p.read_text(errors='replace')}"
                            for p in sorted(folder.rglob("*.txt")))
    spans=reference.get("relevant_text_span") or []
    wanted=set()
    for pair in spans:
        try:wanted.update(range(int(pair[0]),int(pair[1])+1))
        except (ValueError,TypeError,IndexError):continue
    lines="\n".join(p.read_text(errors="replace") for p in sorted(folder.rglob("*.txt"))).splitlines()
    return "\n".join(line for line in lines if any(line.startswith(f"[{i}]") for i in wanted))[:20000]


def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument("--ledger",type=Path,required=True)
    parser.add_argument("--data",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    if args.out.exists():raise ValueError("Review output already exists; use a new directory")
    db=sqlite3.connect(f"file:{args.ledger.resolve()}?mode=ro",uri=True)
    db.row_factory=sqlite3.Row
    manifest=json.loads((args.data/"manifest.json").read_text())
    frozen=json.loads(db.execute("SELECT config_json FROM campaign").fetchone()[0])
    manifest_hash=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
    if frozen["manifest_sha256"]!=manifest_hash:raise ValueError("Dataset differs from the frozen pilot")
    tasks={task["id"]:task for task in manifest["tasks"]}
    pairs=defaultdict(dict)
    for row in db.execute("""SELECT task_id,family,replica,budget_usd,condition,answer
            FROM episodes WHERE condition IN ('script_only','script_llm')
            AND family IN ('file_summary','qmsum') AND task_id LIKE '%-test-%'
            AND status='completed' AND answer IS NOT NULL"""):
        pairs[(row["family"],row["task_id"],row["replica"],row["budget_usd"])][row["condition"]]=row["answer"]
    selected=[];population={}
    for family in ("file_summary","qmsum"):
        eligible=[(key,arms) for key,arms in pairs.items() if key[0]==family and set(arms)=={"script_only","script_llm"}]
        eligible.sort(key=lambda item:_rank(json.dumps(item[0])))
        population[family]=len(eligible)
        selected+=eligible[:math.ceil(.2*len(eligible))]
    if not selected:raise ValueError("No completed pairs to review")
    args.out.mkdir(parents=True)
    key={};records=[]
    for index,(identity,arms) in enumerate(selected):
        family,task_id,replica,budget=identity
        task=tasks[task_id]
        public=json.loads((args.data/task["task"]).read_text())
        reference=json.loads((args.data/task["reference"]).read_text())
        folder=args.data/task["task"].replace("task.json","files")
        swap=int(_rank("order|"+json.dumps(identity)),16)%2==1
        labels=("script_llm","script_only") if swap else ("script_only","script_llm")
        sample_id=f"review-{index+1:03d}"
        key[sample_id]={"task_id":task_id,"replica":replica,"budget_usd":budget,"A":labels[0],"B":labels[1]}
        records.append({"sample_id":sample_id,"family":family,"query":public["prompt"],
                        "source_excerpt":_source(family,folder,reference),"reference":reference,
                        "answer_A":arms[labels[0]],"answer_B":arms[labels[1]]})
    (args.out/"blind-review.jsonl").write_text("".join(json.dumps(record,ensure_ascii=False)+"\n" for record in records))
    (args.out/"condition-key.json").write_text(json.dumps(key,indent=2)+"\n")
    (args.out/"instructions.md").write_text(
        "# ActionBench v2 blind human review\n\n"
        "Two independent reviewers should score A and B without opening condition-key.json. "
        "For each answer, assign integers 0–4 for coverage, factuality, relevance, and evidence. "
        "Use the query, reference and source excerpt; mark insufficient context instead of guessing. "
        "Record scores and a brief reason in a separate file keyed by sample_id. "
        "After both reviewers finish, resolve disagreements without changing the original scores, "
        "then reveal the key and compare paired scores. Missing or failed treatments remain failures "
        "in the full pilot and are excluded only from this answer-quality packet.\n\n"
        f"This outcome-independent hash sample contains {len(records)} complete pairs from "
        f"{population['file_summary']} file-summary and {population['qmsum']} QMSum eligible pairs. "
        "The selection rule was written after the pilot, so treat this review as exploratory.\n")
    print(json.dumps({"sampled_pairs":len(records),"eligible_pairs":population,"manifest_sha256":manifest_hash}))


if __name__=="__main__":main()
