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
actionbench datasets prepare --config experiment.json --manifest manifests/study.json
actionbench smoke --config experiment.json --manifest manifests/study.json
actionbench integration-check --config experiment.json --manifest manifests/study.json
```

`doctor` is local-only. `live-check` makes one real minimal API request and records its cost. Run it deliberately.
`smoke` uses the real Docker graders on correct and incorrect answers. `integration-check` runs a real Docker action that calls the configured model through the broker. Both successful checks are stored with the manifest, harness, and image fingerprints; `create-skills` and `run` refuse stale or missing checks. `live-check` remains an optional direct provider diagnostic.

## Lifecycle

```bash
actionbench create-skills --config experiment.json --manifest manifests/study.json
actionbench run --config experiment.json --manifest manifests/study.json
actionbench status --config experiment.json
actionbench resume --config experiment.json --manifest manifests/study.json
actionbench report --config experiment.json --out artifacts/report.json
actionbench freeze --config experiment.json
```

Every campaign has an immutable configuration hash and, once started, a bound manifest, harness source hash, and image digests. A changed input requires a new campaign name. The current ledger is `actionbench-v3.sqlite3`; it intentionally does not reuse earlier ledgers produced under a different experimental design. Completed evaluations are never rerun by `resume`. To raise a depleted dollar ceiling without changing the fixed experiment configuration, run `actionbench budget --config experiment.json --usd NEW_TOTAL`. This appends an auditable budget update.

If a provider response was lost after submission, inspect the ledger's `unknown_outcome` request. Supply the genuine provider response and its token usage with `actionbench resolve-request --config experiment.json --request-id ID --response-file response.json --evidence 'provider log reference'`. If provider records establish that the request was never executed, use `--confirmed-not-executed` instead of `--response-file`. The resolution and evidence are recorded in the event ledger. Never declare non-execution merely because the response is unavailable.

## Conditions

Every condition receives the same task, model, code interpreter, filesystem, container limits, and episode budget. `skill` receives a conventionally generated skill. `skill_script` receives the exact same skill text plus frozen, deterministic reusable procedures. `improvised` receives that same skill and may write an LLM-calling program during the episode. `action` receives the exact same skill text plus frozen reusable procedures that can request the model only through the harness. Each procedure exposes an identifier, description, and JSON input schema to the agent.

This gives three direct comparisons: `action - skill` tests the full proposal; `action - skill_script` isolates controlled model calls inside reusable procedures from reusable deterministic code; `action - improvised` tests preparation and reuse against writing an equivalent model-calling program during an episode.

## Data contract

`datasets prepare` builds a seeded study with the official EvalPlus MBPP+ v0.1.0 prompts and HotpotQA distractor data, including development and test splits. It records source URLs and SHA-256 hashes in `data/dataset-lock.json`. The MBPP+ image runs EvalPlus and checks that the public prompt exactly matches its official task; the HotpotQA image runs a pinned revision of its official evaluator. Reference material is mounted only into the grader container. Preparing the dataset again replaces the manifest, so finish preparation before creating a campaign.

## Safety and recovery

Generated code runs with no network, credentials, or Docker socket, under resource limits and in a separate workspace for each attempt. The broker alone owns API credentials. A completed request can be reused only when its complete payload hash matches; a changed prompt produces a new request. Unknown provider outcomes are never retried automatically. Package writes use an atomic staging directory, and the ledger verifies package and saved-answer hashes before reuse. Each development evaluation has its own durable episode and budget, so a stopped creation run can resume without silently reusing a shared budget.

The report keeps terminal agent execution failures as zero, keeps in-progress and infrastructure-interrupted work out of quality estimates, resamples benchmark tasks and generated package replicas as separate sources of uncertainty, and reports package-creation cost together with a break-even reuse estimate. Infrastructure interruptions remain resumable; protocol and model-output failures are terminal. A global campaign budget exhaustion leaves episodes queued until the audited ceiling is raised. Submitted provider calls with unknown outcomes are blocked for manual audit, because automatic retry could duplicate a paid request. Only a real Docker `smoke` and provider campaign can support the paper's empirical claim; the local Python tests alone cannot.

If all package revisions fail, `create-skills` records that failure and continues. The resulting held-out treatment episodes receive terminal zero scores, preserving the planned denominator. The report shows package-creation failure rates. Cost comparisons must be read with their paired quality differences: a cheaper condition that solves fewer tasks is a tradeoff, not an efficiency gain. The pilot has 20 test tasks per family and three package replicas; its intervals describe uncertainty but do not establish that the sample is large enough for a confirmatory claim.

After completing the pilot, estimate a prospective sample size for each family with `actionbench plan-sample --config experiment.json --family mbppplus --baseline skill --target-delta 0.10 --target-half-width 0.05 --out artifacts/mbpp-design.json`. The command estimates task, package, and interaction variation from complete paired pilot results and evaluates candidate numbers of tasks and package replicas. These are exploratory normal approximations, especially uncertain with only three package replicas. Choose and freeze a new campaign's sample size before observing its test outcomes.

The scientific design, analyses, reporting requirements, and paper outline are in [PROTOCOL.md](PROTOCOL.md).

To execute the same Docker-based study inside a persistent Vercel Sandbox, see [cloud/README.md](cloud/README.md). A Vercel Function controls the Sandbox; the benchmark itself still runs inside Docker in the Sandbox.
