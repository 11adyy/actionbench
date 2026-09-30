#!/usr/bin/env bash
set -Eeuo pipefail

mkdir -p .cloud-state artifacts
cfg=experiment.json
manifest=manifests/study.json

if [ "$AB_ACTION" = start ] || [ "$AB_ACTION" = canary ]; then
  if [ "${AB_CHECKPOINT_TEST:-}" = 1 ]; then
    cp config.example.json experiment.json
  else
    [ -n "${OPENAI_API_KEY:-}" ] || { echo 'Set OPENAI_API_KEY in GitHub repository Actions secrets' >&2; exit 2; }
    python cloud/configure.py
  fi
elif [ "$AB_ACTION" = resume ] || [ "$AB_ACTION" = resume-canary ] || [ "$AB_ACTION" = raise-budget ]; then
  if { [ "$AB_ACTION" = resume ] || [ "$AB_ACTION" = resume-canary ]; } && [ "${AB_CHECKPOINT_TEST:-}" != 1 ]; then
    [ -n "${OPENAI_API_KEY:-}" ] || { echo 'Set OPENAI_API_KEY in GitHub repository Actions secrets' >&2; exit 2; }
  fi
  [ -s "$cfg" ] || { echo 'Missing restored campaign config' >&2; exit 2; }
  if [ "${AB_CHECKPOINT_TEST:-}" = 1 ]; then
    [ -s artifacts/actionbench-v3.sqlite3 ] || { echo 'SQLite checkpoint was not restored' >&2; exit 2; }
  fi
elif [ "$AB_ACTION" = smoke ]; then
  cfg=config.example.json
else
  echo 'Unknown action' >&2; exit 2
fi

if [ "$AB_ACTION" = raise-budget ]; then
  python -m actionbench.cli budget --config "$cfg" --usd "$AB_BUDGET_USD"
  python -m actionbench.cli status --config "$cfg" > artifacts/status.json
  echo 'Budget ceiling raised. Run resume with the same campaign ID.'
  exit 0
fi

if [ "$AB_ACTION" = resume ] || [ "$AB_ACTION" = resume-canary ]; then
  [ -s .cloud-state/grader-images.tar.gz ] || { echo 'Exact grader images were not cached. Resume is blocked to preserve image fingerprints.' >&2; exit 2; }
  gzip -dc .cloud-state/grader-images.tar.gz | docker load
else
  python -m actionbench.cli images build --config "$cfg"
  docker pull python:3.11-slim
  if [ "$AB_ACTION" = start ] || [ "$AB_ACTION" = canary ]; then
    docker save actionbench-mbppplus:v1 actionbench-hotpot:v1 python:3.11-slim | gzip -1 > .cloud-state/grader-images.tar.gz
  fi
fi

cp "$manifest" .cloud-state/expected-study.json
python -m actionbench.cli datasets prepare --config "$cfg" --out "$manifest"
cmp .cloud-state/expected-study.json "$manifest"
python -m actionbench.cli datasets prepare --config "$cfg" --manifest "$manifest"
python -m actionbench.cli smoke --config "$cfg" --manifest "$manifest"
if [ "$AB_ACTION" = smoke ]; then
  echo 'Real official Docker graders passed correct and incorrect controls.'
  exit 0
fi
if [ "${AB_CHECKPOINT_TEST:-}" = 1 ]; then
  echo 'Real Docker smoke and checkpoint roundtrip passed.'
  exit 0
fi

if [ -f .cloud-state/complete ]; then
  python - <<'PY'
import hashlib, json, sqlite3
from pathlib import Path
marker = json.loads(Path('.cloud-state/complete').read_text())
cfg = Path('experiment.json')
with sqlite3.connect('artifacts/actionbench-v3.sqlite3') as db:
    row = db.execute('SELECT status FROM campaigns WHERE campaign=?', (marker['campaign'],)).fetchone()
assert marker['campaign'] == json.loads(cfg.read_text())['campaign']
assert marker['config_sha256'] == hashlib.sha256(cfg.read_bytes()).hexdigest()
assert marker['source_sha'] == Path('.cloud-state/source-sha').read_text().strip()
assert row and row[0] == 'frozen'
PY
  echo 'Campaign already complete.'
  exit 0
fi

if [ ! -f .cloud-state/broker-passed ]; then
  if [ "$AB_ACTION" = resume ] || [ "$AB_ACTION" = resume-canary ]; then
    blocked=$(python - <<'PY'
import json, sqlite3
from pathlib import Path
cfg = json.loads(Path('experiment.json').read_text())
db = Path('artifacts/actionbench-v3.sqlite3')
if not db.exists():
    print(0)
else:
    with sqlite3.connect(db) as conn:
        print(conn.execute("SELECT COUNT(*) FROM episodes WHERE campaign=? AND status='blocked'", (cfg['campaign'],)).fetchone()[0])
PY
)
    [ "$blocked" = 0 ] || { echo 'Unknown provider outcome is blocked; reconcile the ledger before resuming' >&2; exit 2; }
  fi
  python -m actionbench.cli integration-check --config "$cfg" --manifest "$manifest"
  printf 'passed\n' > .cloud-state/broker-passed
fi

if [ "$AB_ACTION" = canary ] || [ "$AB_ACTION" = resume-canary ]; then
  python -m actionbench.cli create-skills --config "$cfg" --manifest "$manifest"
  python -m actionbench.cli canary --config "$cfg" --manifest "$manifest" > artifacts/canary.json
  python -m actionbench.cli status --config "$cfg" > artifacts/status.json
  python -m actionbench.cli report --config "$cfg" --out artifacts/report.json > /dev/null
  echo 'Generated-package canary passed real Docker and model calls.'
  exit 0
fi

set +e
timeout --signal=INT --kill-after=30s 300m bash -c 'python -m actionbench.cli create-skills --config experiment.json --manifest manifests/study.json && python -m actionbench.cli resume --config experiment.json --manifest manifests/study.json'
code=$?
set -e
if [ "$code" -eq 0 ] && python -m actionbench.cli freeze --config "$cfg"; then
  echo 'Campaign reached a terminal state and was frozen.'
else
  echo 'Campaign is incomplete; state will be archived for resume or diagnosis.'
fi
python -m actionbench.cli status --config "$cfg" > artifacts/status.json
python -m actionbench.cli report --config "$cfg" --out artifacts/report.json > /dev/null
python - <<'PY'
import hashlib, json
from pathlib import Path
cfg = Path('experiment.json')
status = json.loads(Path('artifacts/status.json').read_text())
report = json.loads(Path('artifacts/report.json').read_text())
if status['status'] != report['status']['status']:
    raise SystemExit('Report and status disagree on campaign state')
if status['status'] == 'frozen':
    marker = {'campaign': status['campaign'], 'source_sha': Path('.cloud-state/source-sha').read_text().strip(),
              'config_sha256': hashlib.sha256(cfg.read_bytes()).hexdigest(),
              'validation_status': report['validation_status'], 'scientific_status': report['scientific_status']}
    Path('.cloud-state/complete').write_text(json.dumps(marker, sort_keys=True) + '\n')
summary = Path(__import__('os').environ.get('GITHUB_STEP_SUMMARY', '/dev/null'))
with summary.open('a') as output:
    output.write(f"## ActionBench {status['campaign']}\n\n")
    output.write(f"Campaign: {status['status']} · Validation: {report['validation_status']} · Scientific use: {report['scientific_status']}\n\n")
    output.write(f"Accounted model cost: ${status['accounted_usd']:.4f} of ${status['budget_ceiling_usd']:.2f}.\n\n")
    output.write('Reasons: ' + (', '.join(report['validation_reasons']) or 'none') + '\n')
PY
if [ "$code" -ne 0 ] && [ "$code" -ne 124 ]; then
  echo "Evaluation failed with code $code; durable ledger was archived" >&2
  exit "$code"
fi
