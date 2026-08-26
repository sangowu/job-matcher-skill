# ATS Phase 2 Public API Regression

Date: 2026-08-26. This is a small live contract regression for the production ATS adapters, not a recall benchmark or availability SLA.

## Scope

- 2 Ashby boards, 2 Greenhouse boards, 1 global Lever board, and 1 EU Lever board
- Official public HTTPS GET endpoints only; no API keys, authentication, application POSTs, proxies, or browser automation
- Cross-board concurrency 3, Lever page size 50, maximum 10 sequential pages, 30-second request timeout
- Raw PII-safe evidence: `docs/performance/ats-phase2-public-api-regression.json`

## Result

| Metric | Result |
|---|---:|
| Boards | 6/6 succeeded |
| Requests | 7 |
| Jobs received / normalized | 414 / 414 |
| Unique Provider refs / URL keys | 414 / 414 |
| Strong-identity duplicate rate | 0% |
| Truncated / rate-limited boards | 0 / 0 |
| Wall clock | 5,230.623 ms |

The 79-job global Lever board required two sequential pages at the configured page size, exercising the production pagination path. Weak company/title grouping still collided for 110 records (26.57%), which independently confirms why Provider IDs and `record_id` must remain authoritative.

The artifact contains company/sample board labels and aggregate counts for reproducibility, but no job titles, job URLs, job descriptions, candidate data, credentials, or raw exception text. Public endpoint success can change with external availability; failures are therefore classified and kept independent from Web Search results.
