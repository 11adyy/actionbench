import json
import tempfile
import unittest
from pathlib import Path

from actionbench.config import load_config
from actionbench.errors import ResumeConflict
from actionbench.store import Store


class CoreTests(unittest.TestCase):
    def config(self, root: Path, campaign="c"):
        cfg = json.loads((Path(__file__).parents[1] / "config.example.json").read_text())
        cfg.update({"campaign": campaign, "dataset_root": "data", "artifact_root": "artifacts"})
        path = root / "config.json"; path.write_text(json.dumps(cfg)); return load_config(path)

    def test_campaign_is_configuration_immutable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); cfg = self.config(root); store = Store(cfg.db_path); store.ensure_campaign(cfg)
            raw = json.loads(cfg.source_path.read_text()); raw["budget"]["usd"] = 9; cfg.source_path.write_text(json.dumps(raw))
            with self.assertRaises(ResumeConflict): store.ensure_campaign(load_config(cfg.source_path))

    def test_completed_request_is_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); cfg = self.config(root); store = Store(cfg.db_path); store.ensure_campaign(cfg)
            store.create_episode("e", cfg.campaign, "t", "f", "plain", 0)
            store.reserve_request("r", "e", "s", {"x": 1}, .2); store.mark_submitted("r")
            store.complete_request("r", "p", {"output_text": "OK"}, {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}, .01)
            self.assertEqual(store.request_for_step("e", "s")["state"], "completed")


if __name__ == "__main__":
    unittest.main()
