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

Every campaign has an immutable configuration hash. A changed configuration requires a new campaign name. The current ledger is `actionbench-v3.sqlite3`; it intentionally does not reuse earlier ledgers produced under a different experimental design. Completed evaluations are never rerun by `resume`.

## Conditions

Every condition receives the same task, model, code interpreter, filesystem, container limits, and episode budget. `skill` receives a conventionally generated skill. `skill_script` receives the exact same skill text plus frozen, deterministic reusable procedures. `improvised` receives that same skill and may write an LLM-calling program during the episode. `action` receives the exact same skill text plus frozen reusable procedures that can request the model only through the harness. Each procedure exposes an identifier, description, and JSON input schema to the agent.

This gives three direct comparisons: `action - skill` tests the full proposal; `action - skill_script` isolates controlled model calls inside reusable procedures from reusable deterministic code; `action - improvised` tests preparation and reuse against writing an equivalent model-calling program during an episode.

## Data contract

`datasets prepare` builds a seeded study with the official EvalPlus MBPP+ v0.1.0 prompts and HotpotQA distractor data, including development and test splits. It records source URLs and SHA-256 hashes in `data/dataset-lock.json`. The MBPP+ image runs EvalPlus and checks that the public prompt exactly matches its official task; the HotpotQA image runs a pinned revision of its official evaluator. Reference material is mounted only into the grader container. Preparing the dataset again replaces the manifest, so finish preparation before creating a campaign.

## Safety and recovery

Generated code runs with no network, credentials, or Docker socket, under resource limits and in a persistent per-step workspace. The broker alone owns API credentials. A completed request can be reused only when its complete payload hash matches; a changed prompt produces a new request. Unknown provider outcomes are never retried automatically. Package writes use an atomic staging directory; each development evaluation has its own durable episode and budget, so a stopped creation run can resume without silently reusing a shared budget.

The report keeps terminal execution failures as zero, keeps in-progress work out of quality estimates, resamples benchmark tasks and generated package replicas as separate sources of uncertainty, and reports package-creation cost together with a break-even reuse estimate. A transient infrastructure failure can retry once; protocol, budget, and model-output failures are terminal and remain visible in the result.
