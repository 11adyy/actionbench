from __future__ import annotations

import hashlib
import gzip
import json
import random
import ssl
import urllib.request
from pathlib import Path

from .errors import ActionBenchError

try:
    import certifi
except ImportError as exc:  # surfaced as setup error, never silently weaken TLS
    raise RuntimeError("Install ActionBench dependencies before downloading datasets") from exc


MBPP_URL = "https://github.com/google-research/google-research/raw/master/mbpp/sanitized-mbpp.json"
MBPP_PLUS_URL = "https://github.com/ganler/release/releases/download/humanevalplus/MbppPlus-v0.1.0.jsonl.gz"
MBPP_PLUS_SHA256 = "57d639e77667d88930052b2b07212011c25c0bab94dfc1f19d15e1f7f558bfa9"
HOTPOT_URLS = (
    "http://curtis.ml.cmu.edu/datasets/hotpot/hotpot_dev_distractor_v1.json",
    "https://huggingface.co/datasets/namlh2004/hotpotqa/resolve/main/hotpot_dev_distractor_v1.json?download=true",
)
HOTPOT_SHA256 = "e3da074df24e8369009918aa5cdbdd254dadcde4c63f7569d36afd6f2268caa8"


def _download(urls: str | tuple[str, ...], destination: Path, expected_sha256: str | None = None) -> tuple[str, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    sources = (urls,) if isinstance(urls, str) else urls
    if not destination.exists():
        failures = []
        for url in sources:
            request = urllib.request.Request(url, headers={"User-Agent": "actionbench/0.1"})
            try:
                with urllib.request.urlopen(request, timeout=20, context=ssl.create_default_context(cafile=certifi.where())) as response:
                    payload = response.read()
                digest = hashlib.sha256(payload).hexdigest()
                if expected_sha256 and digest != expected_sha256: raise ActionBenchError(f"Source checksum mismatch for {url}: {digest}")
                destination.write_bytes(payload)
                destination.with_suffix(destination.suffix + ".source.json").write_text(json.dumps({"url": url, "sha256": digest}))
                return url, digest
            except OSError as exc:
                failures.append(f"{url}: {exc}")
        raise ActionBenchError("Could not download benchmark data:\n" + "\n".join(failures))
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    if expected_sha256 and digest != expected_sha256: raise ActionBenchError(f"Existing dataset checksum mismatch: {destination}")
    source_file = destination.with_suffix(destination.suffix + ".source.json")
    if source_file.exists():
        saved = json.loads(source_file.read_text())
        if saved.get("sha256") == digest and saved.get("url") in sources: return saved["url"], digest
    # A pre-sidecar file cannot prove which mirror supplied it. Prefer the
    # immutable checksum-verified mirror over claiming a possibly unavailable URL.
    return sources[-1], digest


def _choose(rows: list[dict], count: int, seed: int) -> list[dict]:
    if len(rows) < count: raise ActionBenchError(f"Dataset has only {len(rows)} records; need {count}")
    order = list(range(len(rows))); random.Random(seed).shuffle(order)
    return [rows[index] for index in order[:count]]


def prepare_study(dataset_root: Path, manifest_path: Path, *, development_per_family: int = 6, test_per_family: int = 20, seed: int = 20260928) -> dict:
    """Fetch public benchmark data and materialize a fixed, hash-recorded study split."""
    raw = dataset_root / "raw"
    mbpp_path = raw / "MbppPlus-v0.1.0.jsonl.gz"; hotpot_path = raw / "hotpot_dev_distractor_v1.json"
    mbpp_source, mbpp_sha = _download(MBPP_PLUS_URL, mbpp_path, MBPP_PLUS_SHA256)
    hotpot_source, hotpot_sha = _download(HOTPOT_URLS, hotpot_path, HOTPOT_SHA256)
    lock = {"mbpp_plus_v0.1.0": {"url": mbpp_source, "sha256": mbpp_sha}, "hotpot_dev_distractor": {"url": hotpot_source, "sha256": hotpot_sha}, "seed": seed}
    mbpp_rows = [json.loads(line) for line in gzip.decompress(mbpp_path.read_bytes()).decode().splitlines() if line.strip()]
    hotpot_rows = json.loads(hotpot_path.read_text())
    requested = development_per_family + test_per_family
    families = []
    for family, rows, convert, image in (
        ("mbppplus", mbpp_rows, _mbpp_record, "actionbench-mbppplus:v1"),
        ("hotpotqa", hotpot_rows, _hotpot_record, "actionbench-hotpot:v1"),
    ):
        picked = _choose(rows, requested, seed + len(family))
        tasks = []
        for index, row in enumerate(picked):
            split = "development" if index < development_per_family else "test"
            task_id = f"{family}-{index:03d}"
            public, reference = convert(row)
            public_rel = Path("public") / family / f"{task_id}.json"
            private_rel = Path("private") / family / task_id
            public_path = dataset_root / public_rel; reference_path = dataset_root / private_rel
            public_path.parent.mkdir(parents=True, exist_ok=True); reference_path.mkdir(parents=True, exist_ok=True)
            public_path.write_text(json.dumps(public, ensure_ascii=False)); (reference_path / "gold.json").write_text(json.dumps(reference, ensure_ascii=False))
            tasks.append({"id": task_id, "split": split, "public_input": public_rel.as_posix(), "public_sha256": hashlib.sha256(public_path.read_bytes()).hexdigest(), "reference_dir": private_rel.as_posix(), "reference_sha256": hashlib.sha256((reference_path / "gold.json").read_bytes()).hexdigest(), "grader": {"image": image, "command": ["python", "/grader/grade.py"]}})
        families.append({"id": family, "creator_brief": _brief(family), "demonstrations": [], "tasks": tasks})
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"name": "actionbench-study-v2", "dataset_lock": lock, "families": families}, indent=2))
    (dataset_root / "dataset-lock.json").write_text(json.dumps(lock, indent=2))
    return {"manifest": str(manifest_path), "lock": lock, "development_tasks": development_per_family * 2, "test_tasks": test_per_family * 2}


def _mbpp_record(row: dict) -> tuple[dict, dict]:
    task_id = str(row["task_id"])
    if not task_id.startswith("Mbpp/") or not isinstance(row.get("prompt"), str) or not isinstance(row.get("entry_point"), str):
        raise ActionBenchError(f"Malformed official MBPP+ task: {task_id}")
    return ({"benchmark": "mbppplus", "evalplus_task_id": task_id, "prompt": row["prompt"], "entry_point": row["entry_point"], "output_contract": "Return only Python source code to append immediately after prompt. Define the requested entry_point."}, {"evalplus_task_id": task_id})


def _hotpot_record(row: dict) -> tuple[dict, dict]:
    context = [{"title": title, "sentences": sentences} for title, sentences in row["context"]]
    return ({"benchmark": "hotpotqa", "id": row["_id"], "question": row["question"], "context": context, "output_contract": {"answer": "string", "sp": "[[title, sentence_index], ...]"}}, {"id": row["_id"], "answer": row["answer"], "supporting_facts": row["supporting_facts"]})


def _brief(family: str) -> str:
    if family == "mbppplus": return "Solve unseen Python programming problems. Produce only a valid implementation matching the requested function contract. Use the code tool for local reasoning and test construction."
    return "Answer unseen multi-hop questions from the supplied passages. Produce the answer plus exact supporting-fact title and sentence-index pairs in the required JSON contract."
