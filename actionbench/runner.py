from __future__ import annotations

import json
import select
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .broker import Broker
from .config import Config
from .errors import ActionBenchError


class ActionRunner:
    def __init__(self, config: Config, broker: Broker):
        self.config, self.broker = config, broker

    def run(self, episode_id: str, action_dir: Path, command: list[str], input_data: dict) -> dict:
        if not shutil.which("docker"):
            raise ActionBenchError("Docker is required to run generated actions safely")
        manifest = json.loads((action_dir / "action.json").read_text())
        if manifest.get("command") != command:
            raise ActionBenchError("Action command does not match its frozen manifest")
        with tempfile.TemporaryDirectory(prefix="actionbench-") as scratch:
            work = Path(scratch)
            stdin_payload = json.dumps({"kind": "input", "input": input_data}) + "\n"
            docker = ["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--pids-limit", "128", "--memory", f"{self.config.execution.memory_mb}m", "--cpus", str(self.config.execution.cpus), "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m", "-v", f"{action_dir.resolve()}:/action:ro", "-v", f"{work.resolve()}:/work:rw", "-w", "/work", self.config.execution.docker_image, *command]
            proc = subprocess.Popen(docker, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            assert proc.stdin and proc.stdout
            proc.stdin.write(stdin_payload); proc.stdin.flush()
            start = time.monotonic()
            try:
                while True:
                    if time.monotonic() - start > self.config.execution.timeout_seconds:
                        proc.kill(); raise ActionBenchError("Action exceeded wall-clock limit")
                    ready, _, _ = select.select([proc.stdout], [], [], 0.25)
                    if not ready:
                        if proc.poll() is not None: break
                        continue
                    line = proc.stdout.readline()
                    if not line:
                        break
                    message = json.loads(line)
                    if message.get("kind") == "llm_request":
                        result = self.broker.call(episode_id, f"{manifest['id']}:{message['step']}", message.get("instructions", ""), message["prompt"], int(message["max_output_tokens"]))
                        proc.stdin.write(json.dumps({"kind": "llm_response", "text": result.text}) + "\n"); proc.stdin.flush()
                    elif message.get("kind") == "result":
                        proc.wait(timeout=5)
                        if proc.returncode:
                            raise ActionBenchError(proc.stderr.read()[:1000])
                        return message["output"]
                stderr = proc.stderr.read()[:1000]
                raise ActionBenchError(f"Action terminated without result: {stderr}")
            finally:
                if proc.poll() is None:
                    proc.kill()
