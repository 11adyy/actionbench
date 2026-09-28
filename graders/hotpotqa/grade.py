import importlib.util
import json
import sys
import tempfile
from pathlib import Path

gold = json.loads(Path('/reference/gold.json').read_text())
try: prediction = json.loads(Path('/submission/submission.txt').read_text())
except json.JSONDecodeError as exc: raise SystemExit(f'Invalid Hotpot prediction JSON: {exc}')
if not isinstance(prediction.get('answer'), str) or not isinstance(prediction.get('sp'), list): raise SystemExit('Prediction must contain answer and sp')
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp); gold_path = root / 'gold.json'; pred_path = root / 'pred.json'
    gold_path.write_text(json.dumps([{'_id': gold['id'], 'answer': gold['answer'], 'supporting_facts': gold['supporting_facts']}]))
    pred_path.write_text(json.dumps({'answer': {gold['id']: prediction['answer']}, 'sp': {gold['id']: prediction['sp']}}))
    spec = importlib.util.spec_from_file_location('hotpot_eval', '/opt/hotpot/hotpot_evaluate_v1.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    metrics = module.eval(str(pred_path), str(gold_path))
    print(json.dumps({'primary': metrics['joint_f1'], 'answer_f1': metrics['f1'], 'support_f1': metrics['sp_f1'], 'official': 'HotpotQA official evaluator'}))
