import json
from pathlib import Path

from evalplus.data import get_mbpp_plus, get_mbpp_plus_hash
from evalplus.eval import SUCCESS
from evalplus.eval._special_oracle import MBPP_OUTPUT_NOT_NONE_TASKS
from evalplus.evaluate import check_correctness, get_groundtruth

task = json.loads(Path('/public/task.json').read_text())
answer = Path('/submission/submission.txt').read_text()
problems = get_mbpp_plus()
problem = problems[task['evalplus_task_id']]
solution = problem['prompt'] + answer
oracle = get_groundtruth(problems, get_mbpp_plus_hash(), MBPP_OUTPUT_NOT_NONE_TASKS)[problem['task_id']]
result = check_correctness('mbpp', 0, problem, solution, oracle, fast_check=False)
base = result['base'][0] == SUCCESS
plus = result['plus'][0] == SUCCESS
print(json.dumps({'primary': int(base and plus), 'base_pass': base, 'plus_pass': plus, 'official': 'EvalPlus MBPP+'}))
