from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .broker import Broker
from .config import Config
from .errors import ActionBenchError, InfrastructureError


class ContainerRunner:
    """Runs agent-written Python in the same isolated environment for every arm."""

    def __init__(self, config: Config, broker: Broker):
        self.config, self.broker = config, broker

    def _docker(self, workspace: Path, command: list[str], action_dir: Path | None, container_name: str) -> list[str]:
        if not shutil.which("docker"):
            raise InfrastructureError("Docker is required for isolated execution")
        mounts = ["-v", f"{workspace.resolve()}:/workspace:rw"]
        if action_dir: mounts += ["-v", f"{action_dir.resolve()}:/action:ro"]
        return ["docker", "run", "--rm", "--name", container_name, "-i", "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128", "--memory", f"{self.config.execution.memory_mb}m", "--cpus", str(self.config.execution.cpus), "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m", *mounts, "-w", "/workspace", self.config.execution.docker_image, *command]

    def execute(self, episode_id: str, run_id: str, workspace: Path, command: list[str], input_data: dict, action_dir: Path | None = None, allow_llm: bool = False) -> dict:
        workspace.mkdir(parents=True, exist_ok=True)
        container_name = f"actionbench-{uuid.uuid4().hex}"
        try:
            proc = subprocess.Popen(self._docker(workspace, command, action_dir, container_name), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise InfrastructureError(f"Cannot start Docker: {exc}") from exc
        assert proc.stdin and proc.stdout and proc.stderr
        deadline = time.monotonic() + self.config.execution.timeout_seconds
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        selector.register(proc.stderr, selectors.EVENT_READ)
        pending = bytearray(); stderr = bytearray(); total_stdout = 0; output = None
        try:
            proc.stdin.write((json.dumps({"kind": "input", "input": input_data}) + "\n").encode()); proc.stdin.flush()
            while selector.get_map():
                if time.monotonic() >= deadline: raise ActionBenchError("Program exceeded wall-clock limit")
                for key, _ in selector.select(min(.25, max(0, deadline - time.monotonic()))):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj); continue
                    if key.fileobj is proc.stderr:
                        stderr.extend(chunk)
                        if len(stderr) > 65536: raise ActionBenchError("Program exceeded stderr limit")
                        continue
                    total_stdout += len(chunk)
                    if total_stdout > 1_048_576: raise ActionBenchError("Program exceeded stdout limit")
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, remaining = pending.partition(b"\n"); pending = bytearray(remaining)
                        try: message = json.loads(line)
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise ActionBenchError(f"Program emitted invalid JSONL: {line[:200]!r}") from exc
                        if not isinstance(message, dict): raise ActionBenchError("Program message must be a JSON object")
                        if message.get("kind") == "llm_request":
                            if not allow_llm: raise ActionBenchError("This condition cannot request an LLM from generated code")
                            step = str(message.get("step", ""))
                            if not step: raise ActionBenchError("LLM request is missing a stable step id")
                            result = self.broker.call(episode_id, f"{run_id}:{step}", str(message.get("instructions", "")), str(message.get("prompt", "")), int(message.get("max_output_tokens", 0)))
                            proc.stdin.write((json.dumps({"kind": "llm_response", "text": result.text}) + "\n").encode()); proc.stdin.flush()
                        elif message.get("kind") == "result":
                            if output is not None or not isinstance(message.get("output"), dict): raise ActionBenchError("Program emitted duplicate or invalid result")
                            output = message["output"]
                        else: raise ActionBenchError(f"Program emitted unknown message kind: {message.get('kind')}")
            if pending: raise ActionBenchError("Program emitted an unterminated JSONL message")
            proc.wait(timeout=max(.01, deadline - time.monotonic()))
            if proc.returncode == 125: raise InfrastructureError(f"Docker failed: {stderr[:1000].decode(errors='replace')}")
            if proc.returncode: raise ActionBenchError(stderr[:1000].decode(errors="replace"))
            if output is None: raise ActionBenchError("Program terminated without result")
            return output
        finally:
            selector.close()
            if proc.poll() is None:
                proc.kill()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: pass
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe: pipe.close()
            # A killed Docker client may leave its container running. Names are
            # random per invocation so cleanup cannot touch another run.
            if shutil.which("docker"):
                try: subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=10)
                except (OSError, subprocess.TimeoutExpired): pass


class ActionRunner:
    def __init__(self, config: Config, broker: Broker):
        self.config, self.broker = config, broker
        self.container = ContainerRunner(config, broker)

    def run(self, episode_id: str, action_dir: Path, invocation_key: str, input_data: dict, *, allow_llm: bool) -> dict:
        manifest = json.loads((action_dir / "procedure.json").read_text())
        action_id, command = manifest.get("id"), manifest.get("command")
        if not isinstance(action_id, str) or not isinstance(command, list): raise ActionBenchError("Invalid frozen action manifest")
        code_hash = hashlib.sha256((action_dir / "main.py").read_bytes()).hexdigest()
        input_hash = hashlib.sha256(json.dumps(input_data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        run_id = hashlib.sha256(f"{episode_id}|{invocation_key}|{action_id}|{code_hash}|{input_hash}".encode()).hexdigest()[:32]
        workspace_root = self.config.artifact_root / "workspaces" / episode_id / run_id
        run = self.broker.store.create_action_run(run_id, episode_id, invocation_key, action_id, input_hash, str(workspace_root))
        if run["state"] == "completed": return json.loads(run["output_json"])
        if run["state"] == "unknown_outcome": raise ActionBenchError(f"Action run {run_id} requires manual review")
        attempt = self.broker.store.action_run_attempts(run_id) + 1
        workspace = workspace_root / f"attempt-{attempt}"
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
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        input_hash = hashlib.sha256(json.dumps(input_data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        run_id = hashlib.sha256(f"{episode_id}|{invocation_key}|plain|{code_hash}|{input_hash}".encode()).hexdigest()[:32]
        workspace_root = self.config.artifact_root / "workspaces" / episode_id / run_id
        run = self.broker.store.create_action_run(run_id, episode_id, invocation_key, f"plain-{code_hash[:16]}", input_hash, str(workspace_root))
        if run["state"] == "completed": return json.loads(run["output_json"])
        attempt = self.broker.store.action_run_attempts(run_id) + 1
        workspace = workspace_root / f"attempt-{attempt}"
        workspace.mkdir(parents=True, exist_ok=True)
        program = workspace / "program.py"; program.write_text(code)
        self.broker.store.set_action_run(run_id, "running")
        try:
            output = self.container.execute(episode_id, run_id, workspace, ["python", "/workspace/program.py"], input_data, allow_llm=False)
            self.broker.store.set_action_run(run_id, "completed", output=output)
            return output
        except Exception as exc:
            self.broker.store.set_action_run(run_id, "failed", error=str(exc))
            raise
