import json
import tempfile
import unittest
from pathlib import Path

from actionbench.manifest import load_manifest, verify_data


class ManifestTests(unittest.TestCase):
    def test_manifest_requires_real_input_and_reference(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "public").mkdir(); (root / "private").mkdir()
            payload = {"families": [{"id": "code", "creator_brief": "x", "tasks": [{"id": "t", "public_input": "public/nope", "reference_dir": "private/nope", "grader": {"image": "pinned", "command": ["grader"]}}]}]}
            manifest = root / "m.json"; manifest.write_text(json.dumps(payload))
            self.assertEqual(len(verify_data(load_manifest(manifest, root))), 2)


if __name__ == "__main__":
    unittest.main()
