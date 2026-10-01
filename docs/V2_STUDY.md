# ActionBench v2: Deep Agents skill experiment

This is a new exploratory study. The v1 ledger and its negative/inconclusive pilots remain unchanged. The comparison is **a frozen skill with a deterministic Python/LangGraph script** versus **the same SKILL.md with a Python/LangGraph script that can request inference**. Both are used by the same Deep Agent. All inner and outer model calls count toward the same episode and campaign ceilings. Generated scripts run in a Docker container with no network and request the same provider through the host's authenticated, metered model capability; the raw API key stays in the host process.

The workflow has two phases: `create-skills` writes three reusable package replicas per family, then `canary` and `run` use those packages without changing them. Before a package is frozen, its generated script must execute on a development task in the real Docker sandbox. The graph-enabled arm must also complete a brokered provider call. The creator gets one correction opportunity after a failed validation or probe; both attempts and their model costs stay in the ledger. These probes are creation checks, not test scores. `file_summary` skills are reused unchanged on the public QMSum meeting summaries. Deep Agents handles skill discovery and the outer tool loop. Scripts are required to be finite LangGraph workflows, not nested agents.

## Local commands

Install `pip install -e '.[v2]'`. Copy `config.v2.example.json` to a new config file and give it a unique campaign ID. Set `OPENAI_API_KEY` in the environment. Docker must be running.

```sh
actionbench-v2 prepare-custom --config experiment-v2.json
actionbench-v2 add-qmsum --config experiment-v2.json
actionbench-v2 build-image --config experiment-v2.json
actionbench-v2 smoke --config experiment-v2.json
actionbench-v2 create-skills --config experiment-v2.json
actionbench-v2 canary --config experiment-v2.json
actionbench-v2 run --config experiment-v2.json
actionbench-v2 judge --config experiment-v2.json
actionbench-v2 report --config experiment-v2.json --out report-v2.json
```

`resume` visits the same frozen cells and skips completed ones. A submitted call or interrupted cell with an unknown outcome is blocked for audit; it is never silently resent. The SQLite ledger, packages, workspaces, dataset manifest, hashes and report must be archived together. GitHub Actions workflow `evaluate-v2.yml` performs the same Docker/API gate and uploads durable state on success or failure.

## Pilot and interpretation

The frozen pilot selects six held-out tasks from each of file exploration, file summaries and QMSum. Two conditions × three replicas × three episode budgets ($0.003, $0.010, $0.030) produce 324 planned test cells. QMSum source revision and file hashes are pinned in `v2_data.py` and the generated manifest. The source repository describes human reference summaries and relevant transcript spans ([QMSum](https://github.com/Yale-LILY/QMSum)).

The custom exploration metric scores answer fields and supporting paths. The custom summary metric scores fact IDs and source paths. QMSum reports ROUGE-L F1. A condition-blind model judge separately rates coverage, factuality, relevance and evidence. These automated scores are **provisional for natural-language quality** until a human, blinded review of a prespecified 20% sample is complete. Report each family separately; cost includes failed attempts and skill creation is shown separately for amortization. Paired intervals resample independent tasks; QMSum queries from the same meeting stay in one cluster. The intervals are exploratory with only six tasks per family.

The campaign cap is $6.344 and its config includes $1.38588963 in previously accounted experiments, staying under the user's cumulative $8 authorization. Any later campaign must update the prior-accounted amount before dispatch. Running a new study because this one loses would be outcome selection, not confirmation.
