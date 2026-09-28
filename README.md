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
actionbench live-check --config experiment.json
```

`doctor` is local-only. `live-check` makes one real minimal API request and records its cost. Run it deliberately.

## Lifecycle

```bash
actionbench datasets prepare --config experiment.json --manifest manifests/pilot.json
actionbench create-skills --config experiment.json --manifest manifests/pilot.json
actionbench run --config experiment.json --manifest manifests/pilot.json
actionbench status --config experiment.json
actionbench resume --config experiment.json --manifest manifests/pilot.json
actionbench report --config experiment.json --out artifacts/report.json
```

Every campaign has an immutable configuration hash. A changed configuration requires a new campaign name. Completed evaluations are never rerun by `resume`.

## Conditions

`plain` receives the task and tools. `skill` receives an ordinary generated skill. `improvised` receives the ordinary skill and can write temporary code that calls the broker; nothing survives the task. `action` receives a frozen skill package with generated executable actions. All conditions have the same agent model, tool limits, input data, and episode budget.

## Data contract

The benchmark accepts JSON task manifests. Each record identifies a public input directory, an isolated reference directory, and a named grader. Reference material is mounted only into the grader container. The included graders run external official commands when configured; absence of the official evaluator is an error, never a synthetic score.

## Safety and recovery

Generated code runs with no network, no credentials, no Docker socket, resource limits, and a workspace mount. The broker is outside the container and alone owns API credentials. It reserves budget before each request, records confirmed responses transactionally, and marks a request as `unknown_outcome` if the process dies after submission. Such requests are never silently retried.
