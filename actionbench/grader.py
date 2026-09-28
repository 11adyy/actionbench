from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import ActionBenchError
from .manifest import Task


def grade(task: Task, answer: str) -> dict:
    if not shutil.which("docker"): raise ActionBenchError("Docker is required for independent grading")
    with tempfile.TemporaryDirectory(prefix="actionbench-grade-") as temp:
        root = Path(temp); submission = root / "submission"; submission.mkdir(); (submission / "submission.txt").write_text(answer)
        command = [part.format(submission="/submission", reference="/reference", public="/public/task.json") for part in task.grader["command"]]
        docker = ["docker", "run", "--rm", "--network", "none", "--read-only", "--pids-limit", "128", "--memory", "2048m", "--tmpfs", "/tmp:rw,nosuid,size=256m", "-v", f"{submission}:/submission:ro", "-v", f"{task.reference_dir}:/reference:ro", "-v", f"{task.public_input}:/public/task.json:ro", task.grader["image"], *command]
        result = subprocess.run(docker, capture_output=True, text=True, timeout=300)
        if result.returncode != 0: raise ActionBenchError(f"Official grader failed: {result.stderr[:1000]}")
        try: score = json.loads(result.stdout)
        except json.JSONDecodeError as exc: raise ActionBenchError("Official grader must emit one JSON score object") from exc
        if not isinstance(score, dict) or "primary" not in score: raise ActionBenchError("Official grader score needs a primary field")
        return score
