# ATS Phase 2 Fake Baseline

Date: 2026-08-26. This is a local Windows correctness/performance checkpoint for the production ATS pipeline, not a release speedup claim.

## Fixture

- Ashby, Greenhouse, and EU Lever Fake boards per iteration
- 3 successful boards, 3 requests, and 3 emitted jobs per iteration
- 30 measured iterations after 3 warmups
- 15-job cold core fixture, 100-job strong-identity fixture, and 10 Fake Browser sessions retained from the release harness
- No Web Search, live ATS, LLM, or cloud-browser calls
- Comparison baseline: `docs/performance/strong-job-identity-baseline.json`
- Raw evidence: `docs/performance/ats-phase2-fake-baseline.json`

## Result

| Metric | p50 | p95 | Result |
|---|---:|---:|---|
| ATS Fake pipeline | 6.654 ms | 7.867 ms | 3/3 boards, 3/3 jobs on every iteration |
| Core total | 68.128 ms | 79.138 ms | +22.6% / +32.9% versus the earlier identity baseline |
| 100-job identity total | 77.564 ms | 85.760 ms | 100 records preserved; 99 conflicts prevented each run |
| Fake Browser | 72.228 ms | 88.189 ms | 100% success, 10 sessions / 70 actions each run |

The new ATS-only path is small relative to the end-to-end agent workflow, but this synthetic timing does not model network latency. All three pre-existing harness sections (core, identity, and Fake Browser) were slower than the earlier checkpoint on this machine, while the ATS implementation is not imported by the disabled core path. The run therefore does not establish causation, but the observed core regression is recorded rather than discarded. This phase claims bounded ATS behavior and stable output counts, not a deterministic-core speedup; a same-environment release rerun is required before making a broader performance claim.
