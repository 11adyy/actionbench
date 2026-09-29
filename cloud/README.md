# Vercel execution

The Vercel function in `api/control.js` is an authenticated controller. It creates one named, persistent Vercel Sandbox from the GitHub repository. The Sandbox runs the **same ActionBench CLI and Docker graders** used locally. Vercel's HTTP function only starts commands and reads status; it does not grade answers or run generated code itself.

Configure these Production environment variables in the Vercel project:

| Variable | Purpose |
| --- | --- |
| `ACTIONBENCH_CONTROL_TOKEN` | Random secret of at least 32 characters for the controller API. |
| `ACTIONBENCH_SANDBOX_NAME` | Optional stable name; defaults to `actionbench-pilot-001`. Changing it starts a separate campaign filesystem. |
| `OPENAI_API_KEY` | Real provider key; only passed to the benchmark coordinator command, never to generated action containers. |
| `AB_MODEL` | Exact available model ID for the provider in `config.example.json`. |
| `AB_INPUT_USD_PER_MILLION` | Verified input price for that model. |
| `AB_OUTPUT_USD_PER_MILLION` | Verified output price for that model. |
| `AB_CACHED_INPUT_USD_PER_MILLION` | Optional cached-input price; defaults to zero. |
| `AB_BUDGET_USD` | Optional campaign ceiling; defaults to the 8 USD example. |
| `AB_CAMPAIGN` | Optional campaign name; defaults to `pilot-001`. |

First call `prepare`. It installs Docker in the Sandbox, builds both official grader images, downloads and verifies the locked datasets, and runs the real correct/incorrect smoke checks. `run` then repeats the smoke under the configured campaign, performs the real broker integration check, creates skill/action packages, evaluates held-out tasks, writes a report and freezes only a complete campaign. All campaign artifacts and SQLite state live in the persistent Sandbox. `resume` continues after a stopped session; the worker refuses to repeat an uncertain broker integration call automatically. The Sandbox session is limited to 45 minutes on Hobby, so a long campaign may require multiple `resume` calls.

Keep a private `.vercel-control.json` in the repository checkout, with `{"url":"https://YOUR-PROJECT.vercel.app","token":"YOUR-SECRET"}`; it is ignored by Git. Then run:

```bash
node cloud/control.mjs prepare
node cloud/control.mjs status
node cloud/control.mjs run
node cloud/control.mjs resume
node cloud/control.mjs report
```

`status` returns the current phase, source commit and recent worker log. It can resume a stopped Sandbox session to read the persisted files. A run that exhausts the dollar ceiling or has an unknown provider outcome needs ledger reconciliation via the ActionBench CLI inside that same Sandbox; do not launch a replacement campaign from a fresh Sandbox. The controller does not accept arbitrary shell commands or user-supplied repository URLs.

Vercel Sandbox snapshots retain the filesystem between sessions; by default they expire after 30 days without use. Export the report and ledger for long-term research archiving. The Sandbox is configured to retain its latest snapshot. Cloud resource usage and model requests can incur charges; `AB_BUDGET_USD` limits model spend in the ActionBench ledger, not Vercel infrastructure charges.
