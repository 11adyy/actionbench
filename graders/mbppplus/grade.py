import json
import subprocess
import tempfile
from pathlib import Path

task = json.loads(Path('/public/task.json').read_text())
answer = Path('/submission/submission.txt').read_text()
with tempfile.TemporaryDirectory() as temp:
    samples = Path(temp) / 'samples.jsonl'
    samples.write_text(json.dumps({'task_id': task['evalplus_task_id'], 'solution': answer}) + '\n')
    run = subprocess.run(['evalplus.evaluate', '--dataset', 'mbpp', '--samples', str(samples)], capture_output=True, text=True)
    reports = list(Path(temp).rglob('*eval_results*.json'))
    if run.returncode != 0 or not reports:
        raise SystemExit(run.stderr[-2000:] or 'EvalPlus produced no result report')
    report = json.loads(reports[0].read_text())
    record = next(iter(report.values()))
    plus = record.get('plus_status') == 'success'
    base = record.get('base_status') == 'success'
    print(json.dumps({'primary': int(plus), 'base_pass': base, 'plus_pass': plus, 'official': 'EvalPlus MBPP+'}))
