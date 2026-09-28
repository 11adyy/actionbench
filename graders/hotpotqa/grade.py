import ast
import json
import subprocess
import tempfile
from pathlib import Path

gold = json.loads(Path('/reference/gold.json').read_text())
try: prediction = json.loads(Path('/submission/submission.txt').read_text())
except json.JSONDecodeError:
    print(json.dumps({'primary': 0.0, 'answer_f1': 0.0, 'support_f1': 0.0, 'official': 'HotpotQA invalid submission'}))
    raise SystemExit(0)
if not isinstance(prediction, dict) or not isinstance(prediction.get('answer'), str) or not isinstance(prediction.get('sp'), list):
    print(json.dumps({'primary': 0.0, 'answer_f1': 0.0, 'support_f1': 0.0, 'official': 'HotpotQA invalid submission'}))
    raise SystemExit(0)
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp); gold_path = root / 'gold.json'; pred_path = root / 'pred.json'
    gold_path.write_text(json.dumps([{'_id': gold['id'], 'answer': gold['answer'], 'supporting_facts': gold['supporting_facts']}]))
    pred_path.write_text(json.dumps({'answer': {gold['id']: prediction['answer']}, 'sp': {gold['id']: prediction['sp']}}))
    run = subprocess.run(['python', '/opt/hotpot/hotpot_evaluate_v1.py', str(pred_path), str(gold_path)], capture_output=True, text=True)
    if run.returncode: raise SystemExit(run.stderr[-2000:])
    metrics = ast.literal_eval(run.stdout.strip().splitlines()[-1])
    print(json.dumps({'primary': metrics['joint_f1'], 'answer_f1': metrics['f1'], 'support_f1': metrics['sp_f1'], 'official': 'HotpotQA official evaluator'}))
