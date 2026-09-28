"""Real subprocess/protocol tests; Docker smoke is a separate required gate."""

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from actionbench.config import load_config
from actionbench.errors import ActionBenchError
from actionbench.runner import ContainerRunner


class LocalProcessRunner(ContainerRunner):
    def _docker(self, workspace, command, action_dir, container_name):
        return [sys.executable, str(workspace / "program.py")]


class ProcessIntegrationTests(unittest.TestCase):
    def make(self, root, code, seconds=2, broker=None):
        raw = json.loads((Path(__file__).parents[1] / "config.example.json").read_text())
        path = root / "config.json"
        path.write_text(json.dumps(raw))
        config = load_config(path)
        config = replace(config, execution=replace(config.execution, timeout_seconds=seconds))
        program = root / "program.py"
        program.write_text(code)
        return LocalProcessRunner(config, broker), root

    def test_real_jsonl_round_trip(self):
        code = "import sys,json\nx=json.loads(sys.stdin.readline())\nprint(json.dumps({'kind':'result','output':{'echo':x['input']['value']}}),flush=True)\n"
        with tempfile.TemporaryDirectory() as d:
            runner, root = self.make(Path(d), code)
            self.assertEqual(runner.execute("e", "r", root, [], {"value": "hello"}), {"echo": "hello"})

    def test_unterminated_message_cannot_hang_the_coordinator(self):
        code = "import sys,time\nsys.stdout.write('{\\\"kind\\\":\\\"result\\\"')\nsys.stdout.flush()\ntime.sleep(30)\n"
        with tempfile.TemporaryDirectory() as d:
            runner, root = self.make(Path(d), code, seconds=1)
            with self.assertRaisesRegex(ActionBenchError, "wall-clock"):
                runner.execute("e", "r", root, [], {})

    def test_real_llm_pipe_exchange(self):
        code = "import sys,json\nsys.stdin.readline()\nprint(json.dumps({'kind':'llm_request','step':'s','instructions':'i','prompt':'p','max_output_tokens':5}),flush=True)\nx=json.loads(sys.stdin.readline())\nprint(json.dumps({'kind':'result','output':{'text':x['text']}}),flush=True)\n"
        class Broker:
            def call(self, episode, step, instructions, prompt, maximum):
                self.observed = (episode, step, instructions, prompt, maximum)
                return type("Result", (), {"text": "real pipe"})()
        with tempfile.TemporaryDirectory() as d:
            broker = Broker()
            runner, root = self.make(Path(d), code, broker=broker)
            self.assertEqual(runner.execute("e", "r", root, [], {}, allow_llm=True), {"text": "real pipe"})
            self.assertEqual(broker.observed, ("e", "r:s", "i", "p", 5))


if __name__ == "__main__": unittest.main()
