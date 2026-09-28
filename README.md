# ActionBench

ActionBench evaluates an agent that uses ordinary skills against an agent that can reuse **actions**: executable, versioned procedures inside a skill which make controlled LLM calls through the benchmark harness.

It uses real provider requests, real subprocess/container execution, persistent SQLite state, and independent graders. It does not fabricate model outputs or grader scores.

## Setup

Use Python 3.11 or newer, Docker, and an API key for a compatible provider. Copy `config.example.json`, set an actual model, verified current provider prices, and export the key named in `api_key_env`.

```bash
python -m venv .venv
.venv/bin/pip install -e .
cp config.example.json experiment.json
actionbench doctor --config experiment.json
actionbench images build --config experiment.json
actionbench datasets prepare --config experiment.json --out manifests/study.json
actionbench live-check --config experiment.json
```

`doctor` is local-only. `live-check` makes one real minimal API request and records its cost. Run it deliberately.

## Lifecycle

```bash
actionbench create-skills --config experiment.json --manifest manifests/study.json
actionbench run --config experiment.json --manifest manifests/study.json
actionbench status --config experiment.json
actionbench resume --config experiment.json --manifest manifests/study.json
actionbench report --config experiment.json --out artifacts/report.json
```

Every campaign has an immutable configuration hash. A changed configuration requires a new campaign name. The current ledger is `actionbench-v2.sqlite3`; it intentionally does not reuse the incompatible prototype ledger. Completed evaluations are never rerun by `resume`.

## Conditions

Every condition receives the same task, model, code interpreter, filesystem, container limits, and episode budget. `skill` receives a conventionally generated skill. `improvised` receives that same skill and may write an LLM-calling program during the episode. `action` receives a separately generated, frozen package of reusable actions. This isolates reusable prepared actions from ordinary code execution and from improvised programmatic calls.

## Data contract

`datasets prepare` builds a seeded study with MBPP+ and HotpotQA distractor data, including development and test splits. It records source URLs and SHA-256 hashes in `data/dataset-lock.json`. The MBPP+ image runs EvalPlus; the HotpotQA image imports its official evaluator. Reference material is mounted only into the grader container.

## Safety and recovery

Generated code runs with no network, credentials, or Docker socket, under resource limits and in a persistent per-step workspace. The broker alone owns API credentials. A completed request can be reused only when its complete payload hash matches; a changed prompt produces a new request. Unknown provider outcomes are never retried automatically.
