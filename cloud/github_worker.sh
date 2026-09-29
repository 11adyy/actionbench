#!/usr/bin/env bash
set -Eeuo pipefail

mkdir -p .cloud-state artifacts
cfg=experiment.json
manifest=manifests/study.json

if [ "$AB_ACTION" = start ]; then
  if [ "${AB_CHECKPOINT_TEST:-}" = 1 ]; then
    cp config.example.json experiment.json
  else
    [ -n "${OPENAI_API_KEY:-}" ] || { echo 'Set OPENAI_API_KEY in GitHub repository Actions secrets' >&2; exit 2; }
    python cloud/configure.py
  fi
elif [ "$AB_ACTION" = resume ]; then
  if [ "${AB_CHECKPOINT_TEST:-}" != 1 ]; then
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

if [ "$AB_ACTION" = resume ]; then
  [ -s .cloud-state/grader-images.tar.gz ] || { echo 'Exact grader images were not cached. Resume is blocked to preserve image fingerprints.' >&2; exit 2; }
  gzip -dc .cloud-state/grader-images.tar.gz | docker load
else
  python -m actionbench.cli images build --config "$cfg"
  docker pull python:3.11-slim
  if [ "$AB_ACTION" = start ]; then
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
  echo 'Campaign already complete.'
  exit 0
fi

if [ ! -f .cloud-state/broker-passed ]; then
  if [ "$AB_ACTION" = resume ]; then
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

set +e
timeout --signal=INT --kill-after=30s 300m bash -c 'python -m actionbench.cli create-skills --config experiment.json --manifest manifests/study.json && python -m actionbench.cli resume --config experiment.json --manifest manifests/study.json'
code=$?
set -e
if [ "$code" -ne 0 ] && [ "$code" -ne 124 ]; then
  echo "Evaluation interrupted with code $code; durable ledger will be archived" >&2
fi
python -m actionbench.cli status --config "$cfg" > artifacts/status.json
python -m actionbench.cli report --config "$cfg" --out artifacts/report.json || true
if python -m actionbench.cli freeze --config "$cfg"; then
  printf 'complete\n' > .cloud-state/complete
  echo 'Campaign complete and frozen.'
else
  echo 'Campaign incomplete. Download its state artifact or run resume with the same campaign ID.'
fi
