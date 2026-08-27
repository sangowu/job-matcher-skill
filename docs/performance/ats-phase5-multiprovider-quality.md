# ATS Phase 5 multi-provider quality validation

Date: 2026-08-27. Count-only artifact: [`ats-phase5-multiprovider-quality.json`](ats-phase5-multiprovider-quality.json).

## Scope and pre-registered gates

This validation used one public board from each supported provider (Ashby, Greenhouse, and Lever), with three concurrent board workers, one page per board, at most three sampled jobs per provider, and a global budget of three read-only GET requests. The gates were declared before the live run:

| Gate | Threshold | Result |
|---|---:|---:|
| Providers represented | at least 3 | 3 |
| Evaluation contract acceptance | 100% | 100% |
| JD handoff coverage | at least 80% | 100% |
| False-positive rate | at most 15% | 14.29% |
| Direct-job `apply+` rate | at least 75% | 100% |
| Adjacent-job `strong_apply` rate | 0% | 0% |
| Direct minus adjacent mean score | at least 10 points | 26.85 |

All gates passed. The boundary result of one false positive in seven jobs is reported as-is and was not relabelled to create extra margin.

## Collection and JD quality

The three requests returned 291 normalized jobs and 11 title-filter candidates. Seven jobs were selected (one direct-title and six adjacent-title candidates); all seven supplied non-truncated JD text. Collection took 2,672.85 ms and transferred 4,563,606 bytes.

The first diagnostic pass exposed two production-parser defects before scoring:

- Greenhouse `content` can contain entity-escaped HTML, as shown in the official [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html). The parser previously left literal tags in all three sampled Greenhouse JDs. Bounded entity decoding before HTML parsing reduced the three samples from 45,997 markup-bearing characters to 19,149 plain-text characters, with zero remaining tag matches.
- Lever documents requirements/benefits in `lists` and optional closing content in `additionalPlain` in its official [Postings API](https://github.com/lever/postings-api). The adapter previously retained only `descriptionPlain`; the sampled JD grew from 1,346 to 5,840 characters after the missing sections were included, with zero remaining tag matches.

Both issues now have deterministic regressions. The same bounded live sample was recollected only after those tests passed.

## Five-dimensional calibration

The orchestrator manually labelled and scored all seven complete JDs against the same structured English CV profile and the repository's five-dimensional rubric. Contract validation accepted 7/7 results. The semantic labels were one direct job, five adjacent jobs, and one false positive. The direct job scored 92.0; adjacent jobs averaged 65.15, and none was promoted to `strong_apply`.

This is evidence that full ATS text can support useful score separation across all three adapters. It is not a market-wide accuracy estimate: the sample is only seven jobs, contains one direct match, and provider sample counts are 3/3/1.

## Browser and privacy boundaries

All seven ATS records already had usable JD text, so browser fallback was required zero times and invoked zero times. No actual ATS-versus-browser latency or quality A/B was measured. The configured Kernel credential was unavailable, which is recorded as a limitation rather than replaced by simulated external evidence.

The committed report contains counts, provider categories, timing, and aggregate scores only. It contains no CV fields, company names, board tokens, job titles, URLs, API keys, or JD text. Full live samples and manual annotations remain untracked local validation inputs and are removed after the audit.

## Offline performance regression

The fixed 30-iteration offline release harness is retained in [`ats-phase5-offline-regression.json`](ats-phase5-offline-regression.json). Against the Phase 4 snapshot, core total p50/p95 changed from 41.675/46.376 ms to 53.541/60.395 ms (+28.5%/+30.2%). ATS Fake normalization changed from 4.094/5.108 ms to 5.133/5.536 ms (+25.4%/+8.4%), while ATS handoff total changed from 7.408/8.367 ms to 9.393/10.305 ms (+26.8%/+23.2%).

This run does **not** establish a parser-caused regression: merge, update, and render stages that this branch did not change also slowed substantially, and two preceding local reruns showed the same host-wide direction. It does establish that Phase 5 did not demonstrate a performance improvement. Attribution requires a same-host, same-time commit A/B or a stable CI runner.
