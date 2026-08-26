# ATS Phase 4 live JD handoff and quality audit

Date: 2026-08-26. Count-only artifact: [`ats-phase4-live-quality.json`](ats-phase4-live-quality.json).

## Live handoff

One known public ATS board was queried with one read-only request and no Web Search or browser service. The response contained 1,727 normalized jobs; the deterministic profile filter emitted 8 candidates, and all 8 carried complete, non-truncated JD text into evaluation snapshots. The fixed Web record was preserved and one duplicate evaluation was avoided.

This means 8/8 tasks were **eligible to skip** a separate job-page/browser fetch. It is not an observed comparison against eight actual browser sessions: the validation deliberately performed zero browser actions.

## Five-dimensional sample

Three JDs were selected deliberately to cover one direct Applied-AI role, one adjacent AI-backend role, and one direct LLM-application role. The same structured CV profile and five-dimensional rubric were used for all three.

| Result | Count |
|---|---:|
| Accepted contract updates | 3 / 3 |
| Rejected / conflicted | 0 / 0 |
| `strong_apply` | 2 |
| `apply` | 1 |
| Mean overall score | 87.5 |

The direct roles scored 95.0 and 92.5. The adjacent backend role scored 75.0 because its high-throughput inference, Kubernetes, Node.js, PyTorch, NoSQL, and distributed-systems expectations were only partially evidenced by the profile. This is the desired quality behavior: receiving a complete ATS JD did not turn every title-filtered candidate into an inflated top match.

## Privacy and retention checks

- All three completed tasks immediately dropped their transient `jd_text`.
- The canonical job table contained zero raw JD fields.
- The count-only runtime metric log contained none of the sampled JD text.
- The remaining local validation snapshot was deleted after the audit, so no pending JD text was retained.

## Limits

The live sample comes from one board and one company and is purposive, not random. It validates the handoff, rubric discrimination, contract, and retention boundaries; it does not establish market-wide precision or latency. A later multi-provider quality sample is still needed before making a broader recommendation-quality claim.
