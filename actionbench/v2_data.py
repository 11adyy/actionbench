"""Frozen file workloads and read-only grading references for ActionBench v2."""
from __future__ import annotations

import hashlib
import json
import random
import re
import urllib.request
from collections import Counter
from pathlib import Path


def _write_json(path: Path, data: object):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def prepare_custom(root: Path, seed: int = 20261001, development: int = 6, test: int = 60) -> dict:
    """Produce deterministic fixtures. Test seeds and entities are disjoint from development."""
    if root.exists() and any(root.iterdir()):
        raise ValueError("Dataset directory already exists; frozen tasks cannot be rewritten")
    tasks = []
    for family in ("file_exploration", "file_summary"):
        for split, count, offset in (("development", development, 0), ("test", test, 10_000)):
            for index in range(count):
                rng = random.Random((seed + offset) * 100_000 + index * 17 + len(family))
                task_id = f"{family}-{split}-{index:03d}"
                corpus = root / "public" / task_id / "files"
                private = root / "private" / f"{task_id}.json"
                if family == "file_exploration":
                    marker = f"Q{offset+index:05d}"
                    target = f"project-{offset+index:05d}"
                    owner = f"owner-{rng.randrange(10000,99999)}"
                    answer = f"decision-{rng.randrange(10000,99999)}"
                    place = f"records/{rng.randrange(3)}/case-{index}.txt"
                    _write(corpus / place, f"Case {marker}\nProject: {target}\nOwner: {owner}\nFinal decision: {answer}\n")
                    for j in range(12 + index % 19):
                        noise = f"project-{rng.randrange(100000,999999)}"
                        _write(corpus / f"archive/{j//5}/note-{j:02d}.txt", f"Case N{j:03d}\nProject: {noise}\nStatus: pending\nOwner: owner-{j:05d}\n")
                    prompt = f"Find case {marker}. Return JSON with keys owner, decision, and evidence_paths. Evidence paths must be relative to files/."
                    gold = {"owner": owner, "decision": answer, "evidence_paths": [place]}
                else:
                    project = f"project-{offset+index:05d}"
                    facts = []
                    for j in range(4):
                        fact_id = f"F{offset+index:05d}-{j}"
                        value = f"milestone-{rng.randrange(10000,99999)}"
                        rel = f"updates/{j:02d}.txt"
                        _write(corpus / rel, f"Update for {project}\nFact {fact_id}: the team completed {value}.\nContext: this affects the next review.\n")
                        facts.append({"id":fact_id,"value":value,"path":rel})
                    for j in range(6 + index % 11):
                        _write(corpus / f"other/{j:02d}.txt", f"Update for unrelated-{j}-{offset+index}\nNo action is required.\n")
                    prompt = f"Summarize completed work for {project}. Return JSON with summary (prose), fact_ids (array) and evidence_paths (array). Use only supported facts."
                    gold = {"facts": facts}
                _write_json(root / "public" / task_id / "task.json", {"id":task_id,"family":family,"prompt":prompt,"files_dir":"files"})
                _write_json(private,gold)
                tasks.append({"id":task_id,"family":family,"split":split,"task":f"public/{task_id}/task.json","reference":f"private/{task_id}.json"})
    manifest = {"version":2,"seed":seed,"tasks":tasks}
    _write_json(root / "manifest.json",manifest)
    return manifest


QMSUM_BASE = "https://raw.githubusercontent.com/Yale-LILY/QMSum/83d7768c1f2b4dfeb091385d3dc7e239b8e5bb7e/data/ALL/jsonl/"


def add_qmsum(root: Path, *, train_count: int = 6, test_count: int = 60, seed: int = 20261001) -> dict:
    """Pin source bytes; keep each meeting entirely in its official split."""
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if any(t["family"] == "qmsum" for t in manifest["tasks"]):
        raise ValueError("QMSum is already present in the frozen manifest")
    locks = {}
    for split, filename, count in (("development","train.jsonl",train_count),("test","test.jsonl",test_count)):
        import ssl
        import certifi
        with urllib.request.urlopen(QMSUM_BASE + filename, timeout=90, context=ssl.create_default_context(cafile=certifi.where())) as response:
            data = response.read()
        locks[filename] = hashlib.sha256(data).hexdigest()
        records = [json.loads(line) for line in data.decode().splitlines() if line.strip()]
        rng = random.Random(seed + (1 if split == "development" else 2))
        rng.shuffle(records)
        selected = []
        for record in records:
            queries = record.get("specific_query_list") or []
            for query in queries[:2]:
                selected.append((record,query))
                if len(selected) == count: break
            if len(selected) == count: break
        if len(selected) < count: raise ValueError(f"QMSum {split} contains only {len(selected)} eligible meetings")
        for index,(record,query) in enumerate(selected):
            task_id = f"qmsum-{split}-{index:03d}"
            corpus = root / "public" / task_id / "files"
            turns = record["meeting_transcripts"]
            for j in range(0,len(turns),30):
                chunk = turns[j:j+30]
                _write(corpus / f"transcript/part-{j//30:03d}.txt","\n".join(f"[{j+k}] {turn.get('speaker','')}: {turn.get('content','')}" for k,turn in enumerate(chunk)))
            _write_json(root / "public" / task_id / "task.json", {"id":task_id,"family":"qmsum","prompt":query["query"]+" Return a concise summary with relevant evidence paths.","files_dir":"files"})
            _write_json(root / "private" / f"{task_id}.json",{"reference":query["answer"],"relevant_text_span":query.get("relevant_text_span",[])})
            manifest["tasks"].append({"id":task_id,"family":"qmsum","split":split,"task":f"public/{task_id}/task.json","reference":f"private/{task_id}.json"})
    manifest["qmsum_sha256"] = locks
    _write_json(manifest_path,manifest)
    return manifest


def _words(text: str) -> list[str]:
    return re.findall(r"\w+",text.casefold())


def rouge_l_f1(hypothesis: str, reference: str) -> float:
    a,b=_words(hypothesis),_words(reference)
    if not a or not b:return 0.0
    prev=[0]*(len(b)+1)
    for word in a:
        row=[0]
        for j,other in enumerate(b,1):row.append(prev[j-1]+1 if word==other else max(row[-1],prev[j]))
        prev=row
    common=prev[-1]
    return 2*common/(len(a)+len(b))


def grade(family: str, answer: str, reference: dict, files_dir: Path) -> dict:
    try: parsed=json.loads(answer)
    except (ValueError,TypeError): parsed=None
    if family == "file_exploration":
        if not isinstance(parsed,dict):return {"primary":0.0,"reason":"invalid_json"}
        fields=sum(parsed.get(key)==reference[key] for key in ("owner","decision"))/2
        evidence=parsed.get("evidence_paths") or []
        supported=all(isinstance(path,str) and (files_dir/path).is_file() for path in evidence)
        citation=bool(supported and set(reference["evidence_paths"]) & set(evidence))
        return {"primary":fields*(1 if citation else 0.5),"field_accuracy":fields,"evidence_correct":citation}
    if family == "file_summary":
        if not isinstance(parsed,dict):return {"primary":0.0,"reason":"invalid_json"}
        gold={item["id"] for item in reference["facts"]}
        predicted=set(parsed.get("fact_ids") or [])
        supported_paths={item["path"] for item in reference["facts"]}
        cited=set(parsed.get("evidence_paths") or [])
        precision=len(gold & predicted)/len(predicted) if predicted else 0
        recall=len(gold & predicted)/len(gold)
        f1=2*precision*recall/(precision+recall) if precision+recall else 0
        evidence=bool(cited & supported_paths) and all((files_dir/path).is_file() for path in cited if isinstance(path,str))
        return {"primary":f1*(1 if evidence else 0.5),"fact_f1":f1,"evidence_present":evidence,"prose_needs_blind_review":True}
    if family == "qmsum":
        summary=parsed.get("summary","") if isinstance(parsed,dict) else answer
        return {"primary":rouge_l_f1(summary,reference["reference"]),"metric":"rouge_l_f1","semantic_review_required":True}
    raise ValueError(f"Unknown family {family}")
