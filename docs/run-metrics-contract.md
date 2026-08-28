# Run-scoped metrics contract

Status: accepted and implemented for the first observability phase on 2026-08-27.

## Goal

Make performance, cost, and quality experiments auditable before changing the search or ranking pipeline. Each user-facing run has one PII-safe `run_id` that links Web Search, merge/update, subagent, ATS, browser, and round events.

This phase adds measurement only. It does not claim a speed, cost, or quality improvement.

## Event lifecycle

1. `round_timer.py start` creates a random `round-...` identifier and records `run_start` with the skill version, Git revision and tracked-dirty flag when available, and a fingerprint of non-secret `config.json`.
2. Every participating command receives that identifier and records it as `run_id`.
3. Each Web Search result page records one `search` event through `search_metrics.py`; search text, result URLs, titles, and companies are never accepted by that command.
4. `round_timer.py finish` records the round measurement, checks the declared expected operations, and records `run_finish` with only operation names and counts.
5. A finished run missing an expected event is `incomplete`. Runtime health becomes `unknown`, never `healthy`; `summarize_metrics.py --fail-on-breach` returns exit code 2.

The default contract expects `run_start`, `search`, `merge`, and `round`. `update` is added automatically when `--evaluations` is greater than zero. The orchestrator adds `--expect subagent`, `--expect ats`, or `--expect browser` only when those paths were used.

## Cost and usage semantics

Subagent events record requested and effective model/effort, fallback, result counts, and optional token/cost fields. A provider that does not expose usage writes JSON `null`, not zero. `cost_type` distinguishes `actual`, `estimated`, and `unavailable`; summaries report coverage before totals.

Web Search records calls, page number, raw/prefiltered/deduplicated/new/cached counts, duration, and a low-cardinality query slot such as `q1`. The primary efficiency metric is effective new candidates per call.

## Privacy boundary

Allowed linkage and metadata:

- random pipeline `run_id`;
- semantic version, short Git revision, and non-secret config fingerprint;
- low-cardinality operation/model/provider labels;
- counts, durations, tokens, and costs.

Prohibited data includes CV/JD text, CV/profile hashes, search text or hashes, company/title/location, URLs, ATS board tokens, API keys, cookies, browser session identifiers, screenshots, and raw exception text.

## A/B gate for later optimizations

Every optimization must use frozen inputs and alternating A/B order. Report quality, valid jobs, tokens, cost, total/first-result latency, and failures. Reject the change if Precision@15 drops by more than 2 percentage points, valid-job yield falls, or failures materially increase. Otherwise require at least 15% cost reduction or 10% total-latency reduction before claiming an optimization. Offline tests establish repeatability; real-provider runs require explicit data/cost authorization.

## Non-goals

- No Prometheus, database, dashboard service, or telemetry upload.
- No ATS refresh-policy change.
- No browser-provider configuration migration.
- No performance claim from the instrumentation change itself.
