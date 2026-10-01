"""Two-stage, resumable Deep Agents experiment."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path

from .v2_data import add_qmsum, prepare_custom
from .v2_store import Ledger


def load(path: Path) -> dict:
    source=path.resolve()
    cfg=json.loads(source.read_text())
    for name in ("dataset_root","artifact_root"):
        cfg[name]=str((source.parent/cfg[name]).resolve())
    if cfg["provider"]["reasoning_effort"]!="none" or cfg["replicas"]<1:
        raise ValueError("v2 needs reasoning none and at least one package replica")
    if cfg["prior_accounted_usd"]+cfg["campaign_limit_usd"]>cfg["global_limit_usd"]+1e-9:
        raise ValueError("Frozen campaign ceiling exceeds the cumulative spending authorization")
    if len(cfg["budgets_usd"])!=3 or sorted(cfg["budgets_usd"])!=cfg["budgets_usd"]:
        raise ValueError("Three increasing budget points are required")
    return cfg


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("a+") as file:
        fcntl.flock(file,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(file,fcntl.LOCK_UN)


def manifest_for(cfg: dict) -> dict:
    path=Path(cfg["dataset_root"])/"manifest.json"
    data=json.loads(path.read_text())
    if data.get("version")!=2:raise ValueError("Dataset manifest version is not 2")
    for task in data["tasks"]:
        for key in ("task","reference"):
            path=Path(cfg["dataset_root"])/task[key]
            if not path.is_file():raise ValueError(f"Dataset file missing: {path}")
    return data


def frozen_config(cfg:dict,manifest:dict) -> dict:
    result=dict(cfg)
    result["manifest_sha256"]=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
    result["harness_sha256"]=hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("v2_*.py")))).hexdigest()
    return result


def meter_for(cfg, ledger):
    from .v2_model import Meter
    provider=cfg["provider"]
    return Meter(ledger,cfg["campaign_limit_usd"],provider["input_usd_per_million"],provider["cached_input_usd_per_million"],provider["output_usd_per_million"])


def status(ledger: Ledger):
    episodes=ledger.db.execute("SELECT family,condition,budget_usd,status,COUNT(*) n FROM episodes GROUP BY family,condition,budget_usd,status").fetchall()
    packages=ledger.db.execute("SELECT family,condition,status,COUNT(*) n FROM packages GROUP BY family,condition,status").fetchall()
    return {"accounted_usd":ledger.spent(),"packages":[dict(x) for x in packages],"episodes":[dict(x) for x in episodes],
            "model_requests":dict(Counter(x["state"] for x in ledger.db.execute("SELECT state FROM calls")))}


def report(ledger:Ledger):
    result=status(ledger)
    rows=ledger.db.execute("""SELECT family,condition,budget_usd,COUNT(*) n,
        SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed,
        AVG(COALESCE(score,0)) quality,
        AVG(duration_seconds) seconds
        FROM episodes WHERE task_id LIKE '%-test-%' AND condition IN ('script_only','script_llm')
        GROUP BY family,condition,budget_usd ORDER BY family,budget_usd,condition""").fetchall()
    groups=[]
    for row in rows:
        item=dict(row)
        calls=ledger.db.execute("""SELECT COUNT(*) n,COALESCE(SUM(COALESCE(c.actual_usd,c.reserved_usd)),0) usd
            FROM calls c JOIN episodes e ON e.id=c.episode_id
            WHERE e.family=? AND e.condition=? AND e.budget_usd=? AND e.task_id LIKE '%-test-%' AND e.condition IN ('script_only','script_llm')""",
            (item["family"],item["condition"],item["budget_usd"])).fetchone()
        item.update({"model_calls":calls["n"],"accounted_usd":calls["usd"],
                     "mean_usd":calls["usd"]/item["n"] if item["n"] else None})
        groups.append(item)
    result["groups"]=groups
    result["procedure_usage"]=[dict(x) for x in ledger.db.execute("""SELECT e.family,e.condition,COUNT(*) invocations
        FROM invocations i JOIN episodes e ON e.id=i.episode_id
        WHERE i.state='completed' AND e.task_id LIKE '%-test-%' AND e.condition IN ('script_only','script_llm') GROUP BY e.family,e.condition""")]
    result["creation_costs"]=[dict(x) for x in ledger.db.execute("""SELECT e.family,e.condition,COUNT(c.id) calls,
        COALESCE(SUM(COALESCE(c.actual_usd,c.reserved_usd)),0) usd
        FROM episodes e LEFT JOIN calls c ON c.episode_id=e.id
        WHERE e.task_id LIKE 'creation:%' GROUP BY e.family,e.condition""")]
    # Pair the two treatments on the same task, replica and budget; failures
    # are zero quality, while interrupted or unknown episodes invalidate a CI.
    paired=defaultdict(dict)
    for row in ledger.db.execute("""SELECT e.id,e.task_id,e.family,e.replica,e.condition,e.budget_usd,e.status,e.score,
        COALESCE(SUM(COALESCE(c.actual_usd,c.reserved_usd)),0) usd
        FROM episodes e LEFT JOIN calls c ON c.episode_id=e.id
        WHERE e.task_id LIKE '%-test-%' AND e.condition IN ('script_only','script_llm')
        GROUP BY e.id"""):
        paired[(row["family"],row["budget_usd"],row["task_id"],row["replica"])][row["condition"]]=dict(row)
    contrasts=[]
    for family,budget in sorted({(key[0],key[1]) for key in paired}):
        cells=[(key,value) for key,value in paired.items() if key[:2]==(family,budget)]
        valid=all(set(value)=={"script_only","script_llm"} and all(value[k]["status"] in ("completed","failed") for k in value) for _,value in cells)
        task_ids=sorted({key[2] for key,_ in cells})
        deltas=[]
        cost_deltas=[]
        by_task=defaultdict(list)
        for key,value in cells:
            if not valid:break
            quality=float(value["script_llm"]["score"] or 0)-float(value["script_only"]["score"] or 0)
            cost=float(value["script_llm"]["usd"])-float(value["script_only"]["usd"])
            deltas.append(quality);cost_deltas.append(cost);by_task[key[2]].append(quality)
        interval=None
        if valid and task_ids:
            rng=random.Random(20261001)
            samples=[]
            for _ in range(2000):
                picked=[rng.choice(task_ids) for _ in task_ids]
                values=[rng.choice(by_task[task]) for task in picked for _ in range(len(by_task[task]))]
                samples.append(sum(values)/len(values))
            samples.sort()
            interval=[samples[49],samples[1950]]
        contrasts.append({"family":family,"budget_usd":budget,"paired_cells":len(cells),"terminal_pairs":len(deltas),
                          "quality_delta_llm_minus_only":sum(deltas)/len(deltas) if deltas else None,
                          "quality_ci95_exploratory":interval,"runtime_cost_delta_usd":sum(cost_deltas)/len(cost_deltas) if cost_deltas else None,
                          "interpretable":valid})
    result["paired_contrasts"]=contrasts
    result["blind_judge"]=[dict(x) for x in ledger.db.execute("""SELECT e.family,COUNT(*) completed,AVG(e.score) mean_score
        FROM episodes e WHERE e.condition='judge' AND e.status='completed' GROUP BY e.family""")]
    result["scientific_status"]="exploratory_only"
    result["limitations"]=["No blind human review of summary quality is in this automatic report.","The pilot sample is six test tasks per family."]
    return result


def main(argv:list[str]|None=None)->int:
    p=argparse.ArgumentParser(prog="actionbench-v2")
    p.add_argument("command",choices=["prepare-custom","add-qmsum","build-image","create-skills","canary","run","resume","judge","verify-terminal","status","report","smoke"])
    p.add_argument("--config",required=True,type=Path)
    p.add_argument("--out",type=Path)
    args=p.parse_args(argv)
    try:
        cfg=load(args.config)
        data_root=Path(cfg["dataset_root"])
        root=Path(cfg["artifact_root"])
        if args.command=="prepare-custom":
            output=prepare_custom(data_root)
            print(json.dumps({"tasks":len(output["tasks"]),"manifest":str(data_root/"manifest.json")},indent=2));return 0
        if args.command=="add-qmsum":
            output=add_qmsum(data_root)
            print(json.dumps({"tasks":len(output["tasks"]),"qmsum_sha256":output["qmsum_sha256"]},indent=2));return 0
        if args.command=="build-image":
            subprocess.run(["docker","build","-t",cfg["execution"]["docker_image"],"-f","docker/skill-v2.Dockerfile","."],check=True)
            return 0
        manifest=manifest_for(cfg)
        frozen=frozen_config(cfg,manifest)
        with locked(root/"v2.lock"):
            ledger=Ledger(root/"actionbench-v2.sqlite3",cfg["campaign"],frozen)
            try:
                if args.command=="status":output=status(ledger)
                elif args.command=="report":output=report(ledger)
                elif args.command=="verify-terminal":
                    rows=ledger.db.execute("SELECT status,COUNT(*) n FROM episodes WHERE task_id LIKE '%-test-%' AND condition IN ('script_only','script_llm') GROUP BY status").fetchall()
                    counts={row["status"]:row["n"] for row in rows}
                    expected=3*cfg["pilot_tasks_per_family"]*cfg["replicas"]*2*len(cfg["budgets_usd"])
                    if sum(counts.values())!=expected or any(k not in ("completed","failed") for k in counts):
                        raise ValueError(f"Pilot has {sum(counts.values())}/{expected} cells and statuses {counts}; cannot report terminal")
                    output={"terminal":True,"planned":expected,"statuses":counts}
                else:
                    from .v2_runner import create_skill,run_episode
                    meter=meter_for(cfg,ledger)
                    if args.command=="create-skills":
                        for family in ("file_exploration","file_summary"):
                            for replica in range(cfg["replicas"]):
                                for kind in ("script_only","script_llm"):
                                    create_skill(cfg,ledger,meter,manifest,family,replica,kind,root)
                    elif args.command in ("canary","run","resume"):
                        chosen=[]
                        for family in ("file_exploration","file_summary","qmsum"):
                            split="development" if args.command=="canary" else "test"
                            count=1 if args.command=="canary" else cfg["pilot_tasks_per_family"]
                            items=[x for x in manifest["tasks"] if x["family"]==family and x["split"]==split]
                            if len(items)<count:raise ValueError(f"Too few {split} tasks for {family}")
                            chosen+=items[:count]
                        for task in chosen:
                            for replica in (range(1) if args.command=="canary" else range(cfg["replicas"])):
                                for budget in ([cfg["budgets_usd"][-1]] if args.command=="canary" else cfg["budgets_usd"]):
                                    for kind in ("script_only","script_llm"):
                                        run_episode(cfg,ledger,meter,task,replica,kind,float(budget),root)
                    elif args.command=="judge":
                        from .v2_judge import judge_summaries
                        output=judge_summaries(cfg,ledger,manifest)
                    elif args.command=="smoke":
                        from .v2_smoke import smoke
                        output=smoke(cfg,ledger,meter,root)
                    else:raise ValueError(args.command)
                    if args.command not in ("smoke","judge"):output=status(ledger)
                if args.out:args.out.write_text(json.dumps(output,indent=2)+"\n")
                print(json.dumps(output,indent=2));return 0
            finally:ledger.close()
    except (ValueError,OSError,subprocess.CalledProcessError) as exc:
        print(f"error: {exc}",file=sys.stderr);return 2


if __name__=="__main__":raise SystemExit(main())
