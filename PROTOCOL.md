# ActionBench: experimental protocol and paper plan

## Research question

Can a skill augmented with frozen, executable **actions** that call an LLM through a constrained harness improve agent task quality or efficiency over a conventional skill? The unit of treatment is the package available to the task-solving agent. An action is an executable with a declared JSON input schema; its code may request LLM inference only through the broker, which enforces the same campaign and episode limits as the outer agent.

The claim is narrower than “executable skills are new.” [Voyager](https://arxiv.org/abs/2305.16291) already used executable skill libraries; [HASP](https://arxiv.org/abs/2605.17734) describes skill programs that intervene in agent execution; [SkillOps](https://arxiv.org/abs/2605.13716) studies typed skill contracts. [Toolformer](https://arxiv.org/abs/2302.04761) is relevant to model-mediated tool calls. The paper should claim a measured, reproducible comparison of *frozen LLM-calling actions within skills* against explicit baselines, if the data support it. Cite and contrast these systems in related work before making a novelty claim.

## Hypotheses and estimands

Primary: paired task-level quality difference, action minus conventional skill, measured as the mean of each family's official primary grader score. Analyze MBPP+ and HotpotQA separately; do not combine them into one headline number. Null hypothesis: mean difference is zero. Report estimate and crossed task/replica bootstrap 95% interval, including intervals that cross zero. No post-hoc switching of the primary metric.

Secondary: action minus deterministic skill script isolates model access from executable reuse; action minus improvised LLM program tests preparation/reuse versus creating a program during the test episode. These conditions do not perfectly isolate every causal component: package generation can produce different code, and the agent can choose different calls. Report this limitation. Other prespecified outcomes are dollar cost per terminal episode, wall-clock seconds, number of broker calls, terminal failure rate, action invocation rate, and break-even reuse count after package-creation cost. Dollar amounts are computed from recorded tokens and the *configured* price schedule; they are not provider invoices. An unknown provider outcome leaves reserved cost and blocks final inference.

## Design and allocation

Use the pinned `manifests/study.json` with 12 development and 40 held-out test tasks across the two families. Generate three independent package replicas per family. Within each replica and condition, allow at most three package revisions and select the one with the highest mean development score, breaking ties in favor of the earlier revision. For each held-out task and replica, evaluate all five conditions: plain, skill, deterministic skill script, improvised LLM program, and frozen action. The same model, task prompt, agent protocol, tool image, grader, per-episode call/token limits, and campaign budget apply to every condition. The command schedules test episodes by a deterministic hash to reduce order effects; it does not constitute blinded random assignment. Package creation uses development data only. Never inspect held-out task references or outcomes while revising prompts, code, thresholds, or the package selection rule; if this occurs, start a new campaign and disclose the exploratory run.

The selected sample is a *pilot*, not a population-representative benchmark. Forty tasks and three package replicas may yield wide intervals; an interval crossing zero is inconclusive, not proof of equivalence. Before a confirmatory campaign, use `plan-sample` on the complete paired pilot grid to estimate task, package, and interaction variance. Set a minimum meaningful quality difference and desired 95% interval half-width *in advance*; the command reports candidate sample sizes and approximate detection probabilities. Its normal approximation is exploratory, especially with only three pilot packages. Report the calculation and decision, even if it implies that a feasible campaign cannot resolve the question. Repeat on a larger preregistered sample and ideally an additional task family. Do not increase the sample after seeing significance without documenting a new confirmatory campaign.

## Operational gates

1. Fix the study manifest and configuration, including real model and verified nonzero input/output prices. Record provider name, model identifier, date, effective price source, Python/Docker versions, and API settings. Keep credentials out of the repository.
2. Build the pinned grader images and obtain the execution image. Run `datasets prepare`, `datasets prepare --manifest ...` verification, `smoke` with real Docker graders, and `integration-check` through a real Docker action and broker request. The grader gate must score one known correct and one wrong answer for each family. The broker gate must return its actual model text. Both checks must match the campaign's manifest, harness, and image hashes. Never count a mocked grader or broker as either gate.
3. Run `create-skills`, `run`, and `resume` as needed. A single coordinator lock prevents concurrent mutation. A restarted episode reuses a completed request only when its full payload hash matches; a submitted request without a saved response becomes blocked for manual audit. Recover it only with an actual provider response or evidence that it was never executed. Generated programs run in isolated attempt workspaces. Answers are checkpointed before grading.
4. Inspect `status` and `report`. Stop if any requested image, source hash, provider outcome, or grader is unresolved. `freeze` only when every planned test episode is terminal and no episode is blocked. Archive config, manifest, dataset lock, image digests, harness commit, SQLite ledger, packages, answers, report, and command log.
5. Run sensitivity analyses: scored-only quality versus terminal-failure-as-zero, with and without episodes that hit a local budget limit, by task family, and by package replica. Count failed package creation in the planned treatment denominator as a zero-quality outcome and report its rate separately. Report incomplete-pair counts; do not silently drop them. Treat infrastructure interruptions as resumable, not as quality failures.

## Reporting and interpretation

The main table should show, per family and condition: completed/scored, terminal failures including package-creation failures, pending/blocked, mean official score, model calls, duration, accounted cost, and package-creation cost. Pairwise action contrasts need point estimates and task/replica intervals. Interpret cost and quality together; lower spending from more failures does not count as an efficiency gain. Include a flow diagram from planned episodes to graded, terminal failures, blocked, and pending. Show at least two trace-level examples with actual broker requests, action invocations, and grader results, with secrets removed. The discussion should separate a measured gain from its mechanism: the current experiment tests the package and workflow together; it cannot prove that code structure alone caused the gain. If actions underperform, report this plainly.

## Paper structure

1. **Introduction:** define the failure mode of instruction-only skills, the action abstraction, and the exact empirical question.
2. **Related work:** Voyager, Toolformer, HASP, SkillOps, agent tool-use/evaluation literature; state the narrower contribution.
3. **System:** package format, schema, broker protocol, budgets, sandbox, versioning, and restart semantics; include a short real action example.
4. **Methods:** preregistered conditions, task sampling/splits, package generation, comparators, official graders, estimands, crossed bootstrap, cost accounting, and threats to validity.
5. **Results:** per-family quality and efficiency with intervals, failure accounting, action usage, and examples.
6. **Discussion:** what the results support, where preparation cost is amortized, inability to isolate all components, and generalization limits.
7. **Reproducibility appendix:** exact image digests, source hashes, manifest, campaign ledger, smoke outputs, commands, and redacted traces.

No empirical conclusion can be written before the real Docker grader and action-broker gates and a completed provider campaign have run. The included study is a pilot; a confirmatory conclusion also requires a separately frozen sample size and new held-out campaign.
