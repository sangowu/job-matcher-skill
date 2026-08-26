# ATS Phase 3 controlled discovery-to-merge A/B

Date: 2026-08-26

Environment: Windows, Python 3.13.5

Raw count-only artifact: [`ats-phase3-controlled-e2e.json`](ats-phase3-controlled-e2e.json)

## Question

With the Web Search candidate set held fixed, does enabling the bounded ATS enhancement add unique records without losing Web records or scheduling duplicate evaluations?

This is a controlled discovery-to-merge measurement, not a ranking-quality experiment. It uses the production ATS sync, deterministic prefilter, strong-identity merge, and isolated temporary canonical tables for both arms.

## Fixed inputs and limits

- Web-only control: 5 public job candidates collected within 6 Web Search calls.
- ATS arm: the same 5 Web candidates plus 3 discovered public boards, one each for Ashby, Greenhouse, and Lever.
- Limits: 3 boards, 10 requests, 5 Lever pages, page size 50, concurrency 3, timeout 30 seconds.
- Local CV text, profile fields, Web candidates, company names, job titles, URLs, board tokens, API keys, and raw exceptions are excluded from the artifact.

The run command was:

```text
python scripts/benchmark_ats_e2e.py \
  --web-candidates data/ats_phase3_web_candidates.json \
  --profile data/cv/7826385b8369d611.json \
  --output docs/performance/ats-phase3-controlled-e2e.json \
  --web-search-calls 6 --max-boards 3 --max-requests 10 \
  --max-pages 5 --page-size 50 --max-concurrency 3 --timeout-seconds 30
```

The input paths above are local ignored files and are not part of the repository.

## Result

| Metric | Web-only | Web + ATS |
|---|---:|---:|
| Input Web candidates | 5 | 5 |
| Unique canonical records | 5 | 24 |
| Web records preserved | 5 | 5 |
| Merge duration | 27.636 ms | 85.493 ms |

ATS-specific observations:

- Boards discovered / succeeded / failed: 3 / 3 / 0.
- Requests: 5; response bytes: 48,955,686.
- Jobs received / normalized / prefiltered / emitted: 5,492 / 5,492 / 22 / 20.
- Incremental unique records: 19.
- Duplicate evaluations avoided by the shared table: 1.
- ATS sync: 12,568.189 ms; ATS arm discovery-to-merge: 12,653.682 ms.
- Greenhouse content fallback: 1 board. Its `content=true` response crossed the 25 MB guard, so the provider retried the listing without content inside the same request budget.

## Interpretation and limits

The run validates the mechanical path: public Web URLs can discover boards, bounded ATS sync can return merge-ready candidates, the shared strong-identity table can suppress a duplicate, and an ATS failure policy does not remove the Web arm.

It does **not** show that all 19 incremental records are useful recommendations. The 20 ATS-emitted records were not sent through JD scoring or manually relevance-audited in this experiment. It also does **not** measure fewer browser fallbacks: `ats_candidates_with_jd_handoff` is 0 because ATS JD content is not yet handed directly to evaluation workers.

The run was request-bounded but not small in transferred data. About 49 MB was read from only five requests, so response bytes and Greenhouse content fallback remain first-class monitoring signals. No further live calls are required to reproduce unit and CI verification; tests use Fake providers.
