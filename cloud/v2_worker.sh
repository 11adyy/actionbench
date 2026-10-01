#!/usr/bin/env bash
set -Eeuo pipefail

mkdir -p .cloud-state artifacts-v2
case "${1:-}" in
  restore)
    [[ "$AB_CAMPAIGN" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || { echo 'Invalid campaign' >&2; exit 2; }
    artifact_id=$(python cloud/find_state_artifact.py)
    if [ "$AB_ACTION" = resume ]; then
      [ -n "$artifact_id" ] || { echo 'No durable artifact exists for resume' >&2; exit 2; }
      gh api "repos/$GITHUB_REPOSITORY/actions/artifacts/$artifact_id/zip" > .cloud-state/download.zip
      unzip -p .cloud-state/download.zip state.tar.gz | tar -xz -C .
      rm .cloud-state/download.zip
      [ -s .cloud-state/v2-source-sha ] && [ -s experiment-v2.json ] || { echo 'Incomplete saved state' >&2; exit 2; }
      git checkout --detach "$(cat .cloud-state/v2-source-sha)"
    else
      [ -z "$artifact_id" ] || { echo 'Campaign already exists; use resume or a new ID' >&2; exit 2; }
      git rev-parse HEAD > .cloud-state/v2-source-sha
      python - <<'PY'
import json,os
from pathlib import Path
c=json.loads(Path('config.v2.example.json').read_text())
c['campaign']=os.environ['AB_CAMPAIGN']
c['prior_accounted_usd']=float(os.environ['AB_PRIOR'])
c['campaign_limit_usd']=float(os.environ['AB_CEILING'])
assert c['prior_accounted_usd']+c['campaign_limit_usd']<=c['global_limit_usd']
Path('experiment-v2.json').write_text(json.dumps(c,indent=2)+'\n')
PY
    fi
    ;;
  run)
    [ -n "${OPENAI_API_KEY:-}" ] || { echo 'OPENAI_API_KEY repository secret is required' >&2; exit 2; }
    python -m actionbench.v2_cli prepare-custom --config experiment-v2.json
    python -m actionbench.v2_cli add-qmsum --config experiment-v2.json
    python -m actionbench.v2_cli build-image --config experiment-v2.json
    image_id=$(docker image inspect --format '{{.Id}}' actionbench-skill-v2:1)
    if [ -s .cloud-state/v2-image-id ]; then
      [ "$image_id" = "$(cat .cloud-state/v2-image-id)" ] || { echo 'Execution image changed during resume' >&2; exit 2; }
    else
      printf '%s\n' "$image_id" > .cloud-state/v2-image-id
    fi
    python -m actionbench.v2_cli smoke --config experiment-v2.json
    if [ "$AB_ACTION" = smoke ]; then exit 0; fi
    python -m actionbench.v2_cli create-skills --config experiment-v2.json
    python -m actionbench.v2_cli canary --config experiment-v2.json
    if [ "$AB_ACTION" = pilot ] || [ "$AB_ACTION" = resume ]; then
      python -m actionbench.v2_cli resume --config experiment-v2.json
      python -m actionbench.v2_cli judge --config experiment-v2.json
      python -m actionbench.v2_cli verify-terminal --config experiment-v2.json
    fi
    ;;
  finalize)
    if [ -s experiment-v2.json ] && [ -s artifacts-v2/actionbench-v2.sqlite3 ] && [ -s data-v2/manifest.json ]; then
      python -m actionbench.v2_cli status --config experiment-v2.json > artifacts-v2/status.json || true
      python -m actionbench.v2_cli report --config experiment-v2.json > artifacts-v2/report.json || true
    fi
    if [ -s experiment-v2.json ]; then
      paths=(experiment-v2.json artifacts-v2 .cloud-state/v2-source-sha)
      if [ -s .cloud-state/v2-image-id ]; then paths+=(.cloud-state/v2-image-id); fi
      tar -czf .cloud-state/state.tar.gz "${paths[@]}"
    fi
    ;;
  *) echo 'Usage: v2_worker.sh restore|run|finalize' >&2; exit 2 ;;
esac
