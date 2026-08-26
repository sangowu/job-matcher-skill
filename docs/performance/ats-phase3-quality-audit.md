# ATS Phase 3 data-quality audit

Date: 2026-08-26

Count-only artifact: [`ats-phase3-quality-audit.json`](ats-phase3-quality-audit.json)

## Outcome

The first Phase 3 quality gate failed, the title filter was corrected, and the final bounded replay **passed** the defined title-level gate.

The initial three-board run emitted 20 candidates. A manual review found that mobile, Android, iOS, and UI roles were passing only because their product suffix contained `AI`. The filter now treats standalone `ai` as a low-information overlap token while explicitly recognizing meaningful phrases such as `AI evaluation`, `AI systems`, and `agent systems`.

## Before and after

| Metric | Before fix | Final live replay |
|---|---:|---:|
| Candidates emitted | 20 | 8 |
| Target-relevant | 5 | 6 |
| Adjacent/stretch | 3 | 2 |
| False positive | 12 | 0 |
| Strict precision | 25% | 75% |
| Precision including adjacent/stretch | 40% | 100% |
| Alive links | 20/20 | 8/8 |
| Exact-title duplicate excess | 5 | 1 |

The fixed Web control remained 4/5 target-relevant and 5/5 alive. In an isolated merge of the final replay, all five Web records were preserved, seven ATS identities were incremental, and one cross-source duplicate evaluation was avoided. The combined table contained 12 unique records.

## Final bounded replay

- Three of three boards succeeded using five public GET requests.
- 5,493 jobs were received and normalized; eight passed the corrected deterministic filter and all eight were emitted.
- The run read 48,964,089 bytes and took 8,044.627 ms for ATS sync.
- One Greenhouse board used the bounded content-free fallback.
- All eight records exposed description-available metadata and a posting date within 45 days. This metadata does not prove JD content quality.

## Gate decision

The title-level gate passes because:

- ATS strict precision is 75%, above the 70% threshold.
- No reviewed false positive passed solely because `ai` appeared in a product suffix.
- Link-alive rate and Web-record preservation are both 100%.
- Strong-identity deduplication still avoids the known cross-source duplicate.

One coverage warning remains: all eight ATS candidates came from one company and one Provider because the other two successful boards produced no roles matching this profile. An arbitrary company cap was not added because it would discard relevant roles without supplying alternatives; downstream JD scoring/ranking should decide which enter the final report.

This audit is title-level, not full CV-to-JD quality. ATS JD content is still not handed directly to evaluation workers, so five-dimension JD scoring and browser-fallback reduction remain unmeasured.
