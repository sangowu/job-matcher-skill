# Strong Job Identity Baseline

Date: 2026-08-25. This is a local Windows correctness/performance checkpoint for the `record_id` / `identity_keys` migration, not a release claim.

## Fixture

- 15 synthetic jobs per cold isolated run
- 100 same-company/same-title jobs with 100 disjoint Greenhouse IDs per identity run
- 10 measured iterations after 2 warmups
- 10 Fake Browser Provider sessions per iteration at concurrency 2
- No Web Search, live ATS, LLM, or cloud-browser calls
- Comparison baseline: `docs/performance/v2.3.0-browser-provider.json`
- Raw evidence: `docs/performance/strong-job-identity-baseline.json`

## Result

| Metric | v2.3.0 p50 / p95 | Identity branch p50 / p95 | Observed change |
|---|---:|---:|---:|
| merge | 5.615 / 6.911 ms | 5.468 / 6.329 ms | -2.6% / -8.4% |
| update | 32.990 / 35.586 ms | 30.438 / 31.624 ms | -7.7% / -11.1% |
| core total | 60.666 / 64.604 ms | 55.558 / 59.560 ms | -8.4% / -7.8% |

All 15 records were merged, independently updated by `record_id`, and rendered in every iteration. Fake Provider success remained 100% (10 sessions / 70 actions per iteration).

The identity-specific fixture preserved all 100 distinct records and recorded 99 prevented strong-ID conflicts in every iteration. Its merge p50/p95 was 14.273/16.062 ms; merge + update total p50/p95 was 54.696/59.476 ms.

The run shows no local performance regression, but the lower timings are not attributed to the identity code: sub-millisecond filesystem and scheduler variation is material at this fixture size. The defensible result is correctness plus non-regression, not a claimed speedup.
