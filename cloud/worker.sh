#!/usr/bin/env bash
set -Eeuo pipefail

cd /vercel/actionbench
mode="${1:-}"
state_dir=/vercel/actionbench/.cloud-state
mkdir -p "$state_dir"
exec 9>"$state_dir/worker.lock"
if ! flock -n 9; then
  echo 'Another ActionBench worker is already running' >&2
  exit 17
fi
exec >>"$state_dir/worker.log" 2>&1

phase() {
  python3 - "$state_dir/state.json" "$1" "$2" <<'PY'
import datetime, json, os, sys
path, phase, detail = sys.argv[1:]
temp = path + '.tmp'
with open(temp, 'w') as handle:
    json.dump({'phase': phase, 'detail': detail, 'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}, handle)
    handle.write('\n')
os.replace(temp, path)
PY
}

on_exit() {
  code=$?
  if [ "$code" -ne 0 ]; then
    phase failed "${mode} exited with code ${code}; inspect worker.log"
    echo "ActionBench ${mode} failed with code ${code}" >&2
  fi
}
trap on_exit EXIT

ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io python3-venv
  fi
  if ! docker info >/dev/null 2>&1; then
    if ! pgrep -x dockerd >/dev/null 2>&1; then
      sudo dockerd --group "$(id -gn)" >"$state_dir/dockerd.log" 2>&1 &
    fi
    for _ in $(seq 1 90); do
      if docker info >/dev/null 2>&1; then return; fi
      sleep 1
    done
    echo 'Docker daemon did not become ready' >&2
    tail -80 "$state_dir/dockerd.log" >&2 || true
    exit 2
  fi
}

prepare() {
  phase preparing 'Installing Docker and building official graders'
  git rev-parse HEAD >"$state_dir/source-sha"
  ensure_docker
  if [ ! -x .cloud-venv/bin/python ]; then python3 -m venv .cloud-venv; fi
  .cloud-venv/bin/pip install -e .
  .cloud-venv/bin/python -m actionbench.cli images build --config config.example.json
  docker pull python:3.11-slim
  cp manifests/study.json "$state_dir/expected-study.json"
  .cloud-venv/bin/python -m actionbench.cli datasets prepare --config config.example.json --out manifests/study.json
  cmp "$state_dir/expected-study.json" manifests/study.json
  .cloud-venv/bin/python -m actionbench.cli datasets prepare --config config.example.json --manifest manifests/study.json
  python3 - <<'PY'
import json
from pathlib import Path
data = json.loads(Path('config.example.json').read_text())
data['campaign'] = 'cloud-smoke'
data['dataset_root'] = '/vercel/actionbench/data'
data['artifact_root'] = '/vercel/actionbench/.cloud-state/smoke-artifacts'
Path('.cloud-state/smoke.json').write_text(json.dumps(data))
PY
  .cloud-venv/bin/python -m actionbench.cli smoke --config "$state_dir/smoke.json" --manifest manifests/study.json
  printf 'ready\n' >"$state_dir/ready"
  phase ready 'Real Docker graders passed their correct/incorrect smoke tests'
}

gate_status() {
  python3 - <<'PY'
import json, sqlite3
from pathlib import Path
cfg = json.loads(Path('experiment.json').read_text())
conn = sqlite3.connect('artifacts/actionbench-v3.sqlite3')
blocked = conn.execute("SELECT COUNT(*) FROM episodes WHERE campaign=? AND status='blocked'", (cfg['campaign'],)).fetchone()[0]
if blocked:
    print('blocked')
else:
    present = conn.execute("SELECT 1 FROM validation_gates WHERE campaign=? AND kind='action_broker'", (cfg['campaign'],)).fetchone()
    print('present' if present else 'absent')
PY
}

evaluate() {
  if [ ! -f "$state_dir/ready" ]; then echo 'Run prepare first' >&2; exit 2; fi
  if [ -f "$state_dir/complete" ]; then phase complete 'Campaign already frozen'; return; fi
  ensure_docker
  python3 cloud/configure.py
  if [ -z "${OPENAI_API_KEY:-}" ]; then echo 'OPENAI_API_KEY is required' >&2; exit 2; fi
  phase running 'Checking real Docker graders and provider broker'
  .cloud-venv/bin/python -m actionbench.cli smoke --config experiment.json --manifest manifests/study.json
  gate=$(gate_status)
  if [ "$gate" = blocked ]; then
    echo 'Provider outcome is blocked; reconcile it before resuming' >&2
    exit 2
  fi
  if [ "$gate" = absent ]; then
    if [ "$mode" = resume ]; then
      echo 'Broker gate is missing. Inspect the provider ledger, then explicitly use run.' >&2
      exit 2
    fi
    .cloud-venv/bin/python -m actionbench.cli integration-check --config experiment.json --manifest manifests/study.json
  fi
  phase running 'Creating skill/action packages and evaluating held-out tasks'
  .cloud-venv/bin/python -m actionbench.cli create-skills --config experiment.json --manifest manifests/study.json
  .cloud-venv/bin/python -m actionbench.cli resume --config experiment.json --manifest manifests/study.json
  .cloud-venv/bin/python -m actionbench.cli report --config experiment.json --out artifacts/report.json
  .cloud-venv/bin/python -m actionbench.cli freeze --config experiment.json
  printf 'complete\n' >"$state_dir/complete"
  phase complete 'All planned episodes have terminal outcomes and report is ready'
}

case "$mode" in
  prepare) prepare ;;
  run|resume) evaluate ;;
  *) echo 'Usage: worker.sh prepare|run|resume' >&2; exit 2 ;;
esac
