import json
import tempfile
import unittest
from pathlib import Path

from actionbench.datasets import _mbpp_record, MBPP_PLUS_SHA256

from actionbench.manifest import load_manifest, verify_data


class ManifestTests(unittest.TestCase):
    def test_mbpp_public_prompt_is_the_official_evalplus_prompt(self):
        source = Path(__file__).parents[1] / "data" / "raw" / "MbppPlus-v0.1.0.jsonl.gz"
        if not source.exists(): self.skipTest("Official dataset has not been prepared")
        import gzip
        import hashlib
        payload = source.read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), MBPP_PLUS_SHA256)
        rows = {row["task_id"]: row for row in (json.loads(line) for line in gzip.decompress(payload).decode().splitlines())}
        manifest = json.loads((Path(__file__).parents[1] / "manifests" / "study.json").read_text())
        for family in manifest["families"]:
            if family["id"] != "mbppplus": continue
            for task in family["tasks"]:
                public = json.loads((Path(__file__).parents[1] / "data" / task["public_input"]).read_text())
                official = rows[public["evalplus_task_id"]]
                self.assertEqual(public["prompt"], official["prompt"])
                self.assertEqual(public["entry_point"], official["entry_point"])
    def test_manifest_requires_real_input_and_reference(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "public").mkdir(); (root / "private").mkdir()
            payload = {"families": [{"id": "code", "creator_brief": "x", "tasks": [{"id": "t", "public_input": "public/nope", "reference_dir": "private/nope", "grader": {"image": "pinned", "command": ["grader"]}}]}]}
            manifest = root / "m.json"; manifest.write_text(json.dumps(payload))
            self.assertEqual(len(verify_data(load_manifest(manifest, root))), 2)


if __name__ == "__main__":
    unittest.main()
