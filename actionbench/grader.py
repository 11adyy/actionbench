from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import InfrastructureError
from .manifest import Task


def grade(task: Task, answer: str) -> dict:
    if not shutil.which("docker"): raise InfrastructureError("Docker is required for independent grading")
    with tempfile.TemporaryDirectory(prefix="actionbench-grade-") as temp:
        root = Path(temp); submission = root / "submission"; submission.mkdir(); (submission / "submission.txt").write_text(answer)
        command = [part.format(submission="/submission", reference="/reference", public="/public/task.json") for part in task.grader["command"]]
        docker = ["docker", "run", "--rm", "--network", "none", "--read-only", "--pids-limit", "128", "--memory", "2048m", "--tmpfs", "/tmp:rw,nosuid,size=256m", "-v", f"{submission}:/submission:ro", "-v", f"{task.reference_dir}:/reference:ro", "-v", f"{task.public_input}:/public/task.json:ro", task.grader["image"], *command]
        try: result = subprocess.run(docker, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc: raise InfrastructureError(f"Grader container unavailable: {exc}") from exc
        if result.returncode != 0: raise InfrastructureError(f"Official grader failed: {result.stderr[:1000]}")
        try: score = json.loads(result.stdout)
        except json.JSONDecodeError as exc: raise InfrastructureError(f"Official grader emitted non-JSON: {result.stdout[:250]}") from exc
        if not isinstance(score, dict) or "primary" not in score: raise InfrastructureError("Official grader score needs a primary field")
        return score
