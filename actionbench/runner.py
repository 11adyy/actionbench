from __future__ import annotations

import hashlib
import json
import select
import shutil
import subprocess
import time
from pathlib import Path

from .broker import Broker
from .config import Config
from .errors import ActionBenchError


class ContainerRunner:
    """Runs agent-written Python in the same isolated environment for every arm."""

    def __init__(self, config: Config, broker: Broker):
        self.config, self.broker = config, broker

    def _docker(self, workspace: Path, command: list[str], action_dir: Path | None) -> list[str]:
        if not shutil.which("docker"):
            raise ActionBenchError("Docker is required for isolated execution")
        mounts = ["-v", f"{workspace.resolve()}:/workspace:rw"]
        if action_dir: mounts += ["-v", f"{action_dir.resolve()}:/action:ro"]
        return ["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--pids-limit", "128", "--memory", f"{self.config.execution.memory_mb}m", "--cpus", str(self.config.execution.cpus), "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m", *mounts, "-w", "/workspace", self.config.execution.docker_image, *command]

    def execute(self, episode_id: str, run_id: str, workspace: Path, command: list[str], input_data: dict, action_dir: Path | None = None, allow_llm: bool = False) -> dict:
        workspace.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(self._docker(workspace, command, action_dir), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        assert proc.stdin and proc.stdout
        proc.stdin.write(json.dumps({"kind": "input", "input": input_data}) + "\n"); proc.stdin.flush()
        start = time.monotonic()
        try:
            while True:
                if time.monotonic() - start > self.config.execution.timeout_seconds:
                    proc.kill(); raise ActionBenchError("Program exceeded wall-clock limit")
                ready, _, _ = select.select([proc.stdout], [], [], 0.25)
                if not ready:
                    if proc.poll() is not None: break
                    continue
                line = proc.stdout.readline()
                if not line: break
                try: message = json.loads(line)
                except json.JSONDecodeError as exc: raise ActionBenchError(f"Program emitted invalid JSONL: {line[:200]!r}") from exc
                if message.get("kind") == "llm_request":
                    if not allow_llm: raise ActionBenchError("This condition cannot request an LLM from generated code")
                    step = str(message.get("step", ""))
                    if not step: raise ActionBenchError("LLM request is missing a stable step id")
                    result = self.broker.call(episode_id, f"{run_id}:{step}", str(message.get("instructions", "")), str(message.get("prompt", "")), int(message.get("max_output_tokens", 0)))
                    proc.stdin.write(json.dumps({"kind": "llm_response", "text": result.text}) + "\n"); proc.stdin.flush()
                elif message.get("kind") == "result":
                    proc.wait(timeout=5)
                    if proc.returncode: raise ActionBenchError(proc.stderr.read()[:1000])
                    output = message.get("output")
                    if not isinstance(output, dict): raise ActionBenchError("Program result must be an object")
                    return output
            raise ActionBenchError(f"Program terminated without result: {proc.stderr.read()[:1000]}")
        finally:
            if proc.poll() is None: proc.kill()


class ActionRunner:
    def __init__(self, config: Config, broker: Broker):
        self.config, self.broker = config, broker
        self.container = ContainerRunner(config, broker)

    def run(self, episode_id: str, action_dir: Path, invocation_key: str, input_data: dict, *, allow_llm: bool) -> dict:
        manifest = json.loads((action_dir / "procedure.json").read_text())
        action_id, command = manifest.get("id"), manifest.get("command")
        if not isinstance(action_id, str) or not isinstance(command, list): raise ActionBenchError("Invalid frozen action manifest")
        input_hash = hashlib.sha256(json.dumps(input_data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        run_id = hashlib.sha256(f"{episode_id}|{invocation_key}|{action_id}|{input_hash}".encode()).hexdigest()[:32]
        workspace = self.config.artifact_root / "workspaces" / episode_id / run_id
        run = self.broker.store.create_action_run(run_id, episode_id, invocation_key, action_id, input_hash, str(workspace))
        if run["state"] == "completed": return json.loads(run["output_json"])
        if run["state"] == "unknown_outcome": raise ActionBenchError(f"Action run {run_id} requires manual review")
        self.broker.store.set_action_run(run_id, "running")
        try:
            output = self.container.execute(episode_id, run_id, workspace, command, input_data, action_dir, allow_llm=allow_llm)
            self.broker.store.set_action_run(run_id, "completed", output=output)
            return output
        except Exception as exc:
            self.broker.store.set_action_run(run_id, "failed", error=str(exc))
            raise

    def run_ephemeral_llm_program(self, episode_id: str, invocation_key: str, code: str, input_data: dict) -> dict:
        if "from action_sdk import ActionContext" not in code: raise ActionBenchError("Programmatic LLM code must use ActionContext")
        code_hash = hashlib.sha256(code.encode()).hexdigest()[:16]
        root = self.config.artifact_root / "ephemeral" / episode_id / f"{invocation_key}-{code_hash}"
        root.mkdir(parents=True, exist_ok=True)
        (root / "main.py").write_text(code)
        from . import action_sdk
        (root / "action_sdk.py").write_text(Path(action_sdk.__file__).read_text())
        (root / "procedure.json").write_text(json.dumps({"id": f"improvised-{code_hash}", "description": "Ephemeral model-written LLM procedure.", "input_schema": {"type": "object"}, "command": ["python", "/action/main.py"]}))
        return self.run(episode_id, root, invocation_key, input_data, allow_llm=True)

    def run_plain_program(self, episode_id: str, invocation_key: str, code: str, input_data: dict) -> dict:
        if "llm_request" in code or "action_sdk" in code: raise ActionBenchError("Plain code tool cannot access the LLM protocol")
        root = self.config.artifact_root / "workspaces" / episode_id / "tools"
        root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(f"{invocation_key}|{code}".encode()).hexdigest()[:16]
        program = root / f"{digest}.py"; program.write_text(code)
        return self.container.execute(episode_id, f"tool-{digest}", root, ["python", f"/workspace/{digest}.py"], input_data, allow_llm=False)
