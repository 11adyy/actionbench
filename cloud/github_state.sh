#!/usr/bin/env bash
set -Eeuo pipefail

mkdir -p .cloud-state
case "${1:-}" in
  restore)
    [[ "$AB_CAMPAIGN" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || { echo 'Invalid campaign ID' >&2; exit 2; }
    prefix="actionbench-state-${AB_CAMPAIGN}-"
    artifact_id=''
    if [ "$AB_ACTION" != smoke ]; then
      artifact_id=$(gh api "repos/$GITHUB_REPOSITORY/actions/artifacts?per_page=100" --jq ".artifacts | map(select(.expired == false and (.name | startswith(\"$prefix\")))) | sort_by(.created_at) | last | .id // empty")
    fi
    if [ "$AB_ACTION" = resume ]; then
      [ -n "$artifact_id" ] || { echo 'No saved campaign artifact exists to resume' >&2; exit 2; }
      gh api "repos/$GITHUB_REPOSITORY/actions/artifacts/$artifact_id/zip" > .cloud-state/download.zip
      unzip -p .cloud-state/download.zip state.tar.gz | tar -xz -C .
      rm .cloud-state/download.zip
      [ -s .cloud-state/source-sha ] && [ -s experiment.json ] || { echo 'Saved campaign state is incomplete' >&2; exit 2; }
      source_sha=$(cat .cloud-state/source-sha)
      git checkout --detach "$source_sha"
    else
      [ -z "$artifact_id" ] || { echo 'Campaign already has saved state; use resume or a new campaign ID' >&2; exit 2; }
      source_sha=$(git rev-parse HEAD)
      if [ "$AB_ACTION" = start ]; then printf '%s\n' "$source_sha" > .cloud-state/source-sha; fi
    fi
    printf 'source_sha=%s\n' "$source_sha" >> "$GITHUB_OUTPUT"
    ;;
  pack)
    if [ ! -s experiment.json ]; then echo 'No campaign config to save'; exit 0; fi
    mkdir -p artifacts
    tar --exclude=state.tar.gz --exclude=grader-images.tar.gz -czf .cloud-state/state.tar.gz experiment.json artifacts .cloud-state
    ;;
  *) echo 'Usage: github_state.sh restore|pack' >&2; exit 2 ;;
esac
