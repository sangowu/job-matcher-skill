# Run metrics real smoke — 2026-08-28

This checkpoint exercises the current branch through one small real pipeline run. It validates instrumentation and completeness; it is not an A/B performance claim.

## Scope

- One Web Search call, four selected candidates, and one evaluation worker.
- ATS company discovery ran, but ATS synchronization was disabled.
- Browser-provider automation was disabled.
- The disposable run used an isolated data directory; no installed-skill or user cache was modified.

## Result

| Metric | Observed |
|---|---:|
| Raw / prefiltered / deduplicated search results | 12 / 7 / 6 |
| New candidates | 4 |
| Search duration | 1,845 ms |
| Evaluations updated / rejected / conflicted | 4 / 0 / 0 |
| Evaluation success rate | 100% |
| Full-JD / snippet-scored results | 4 / 0 |
| Verified alive / unverified | 3 / 1 |
| Evaluation worker duration | 195,821 ms |
| End-to-end round duration | 496,307.99 ms |
| Metrics events / failures | 7 / 0 |
| Completeness / health | complete / healthy |
| Threshold breaches | 0 |

The seven schema-v5 operations were `run_start`, `search`, `merge`, `subagent`, `update`, `round`, and `run_finish`, all tied to one pipeline run identifier. The final queue had zero pending tasks.

The evaluation worker used `gpt-5.6-terra` with `high` reasoning effort and produced four contract-valid results. Runtime token usage and cost were not exposed, so both are recorded as `unknown`/`null`, not zero.

The metrics file was checked for CV/profile hashes, selected company names, and URLs; none were present. Candidate details remained confined to disposable run artifacts.
