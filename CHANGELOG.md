# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.2.0] - 2026-08-08

Detailed release notes: [docs/releases/v2.2.0.md](docs/releases/v2.2.0.md).

### Added

- `scripts/round_timer.py` and a PII-safe `round` metric event (`round_duration_ms`, `orchestration`, `batches`, `evaluations`, `jobs_reported`) that time a full matching round, making the serial-vs-overlapped comparison measurable instead of merely modeled. Round durations are excluded from script-level `duration_ms` percentiles; `summarize_metrics.py` reports per-mode p50/p95 and `overlap_saving_pct`.
- Overlapped orchestration guidance in `WORKFLOW.md` and `SKILL.md`: batch N evaluation workers and batch N+1 search workers are spawned in the same message, backed by the existing eval-run snapshot/conflict machinery; `max_parallel_subagents` is documented as a shared global budget (1 search + 2 evaluation during overlap).
- One-stop precise-ranking worker guidance: fetch JD, extract `jd_profile`, and score inside a single subagent, keeping the full JD text out of the orchestrator context.
- Regional job-platform URL canonicalization (liepin, zhipin, lagou, seek, reed) so cross-source dedup gets exact `url_key` hits outside the international ATS ecosystem.
- Multi-market site strategy in `search_playbook.md` (Ireland/UK, continental Europe, Australia/NZ, mainland China, plus a locale-inference rule for other markets).
- Query-variant dedup rule in `search_playbook.md`: seniority/stack modifiers on the same role no longer spend extra websearch budget.
- LLM fallback rule for closed-posting detection in `scoring_rubric.md`; `_CLOSED_PATTERNS` is now grouped per language for easy extension.

### Changed

- Recommendation thresholds now match JobRadar: `stretch_apply` ≥ 60 (was 55) and `low_priority` ≥ 20 (was 40).
- `scoring_rubric.md` adds deterministic seniority caps (ported from JobRadar's profile guards) and clarifies that hard-filter/deal-breaker hits lower `overall_score` by lowering the affected dimension scores, never by editing the weighted total directly.
- `analysis_contract.py` rejects recommendations more aggressive than the score band (downgrades stay allowed), so an evaluation can no longer pair a low score with `apply`.
- `_CLOSED_PATTERN` covers JobRadar's newer evergreen-posting phrases ("this exact role may not be open", "posting is to advertise potential job opportunities").

### Fixed

- Stale evaluation runs (pending longer than `eval_run_stale_hours`, default 2) and corrupt run manifests are now abandoned during `merge`, releasing jobs that would otherwise stay `in_evaluation` forever; abandoned runs are logged to `data/eval_runs/history.jsonl` and counted as `abandoned_runs` in merge stats.
- Reports no longer fall back to match scores from a different CV/candidate-profile pair; such jobs render unscored with a "needs re-score" badge instead of showing a misleading score.
- `_aggregate_batch` no longer drops a second URL from the same source within one batch (listing page + detail page); extra URLs are kept as `alt_urls` and feed `all_url_keys` for exact matching.
- `_aggregate_batch` now falls back to `url_key` matching after `dedup_key`, so an aggregator re-listing with a rewritten title (same job id in the URL) is collapsed before evaluation is dispatched instead of after, removing one wasted LLM evaluation per occurrence.

### Security

- Escape `</` when embedding job/meta/health JSON into the HTML report so external job content cannot break out of the inline `<script>` block.
- Reject non-http(s) job and source URLs (for example `javascript:`) before they reach report links.
- Prompt-injection guidance in `references/scoring_rubric.md` and `references/search_playbook.md`: search results and JD text are untrusted data; embedded instructions must be ignored.

### Removed

- `scripts/_build_table.py`, a leftover one-off script that bypassed the merge contract and carried hardcoded personal data.

## [2.1.0] - 2026-07-31

Detailed release notes: [docs/releases/v2.1.0.md](docs/releases/v2.1.0.md).

### Added

- PII-safe `data/metrics.jsonl` events for merge/update success and failure paths.
- Runtime duration and lock-wait p50/p95/p99, evaluation rates, queue backlog, and configurable health thresholds.
- `scripts/summarize_metrics.py` for Markdown/JSON health reports and automation-friendly threshold exits.
- Automatic 7-day and 30-day health snapshots embedded in every generated HTML report.
- A header health indicator and full-screen, bilingual monitoring view with KPI cards and threshold alerts.
- Deterministic tests for metric sanitization, summary calculations, failure recording, and concurrent JSONL appends.
- Render integration tests for window isolation, no-data/degraded states, graceful monitoring failure, and PII exclusion.

### Fixed

- `stats.new` now means jobs added by the current merge instead of all rows still carrying `status: new`.

## [2.0.0] - 2026-07-31

Detailed release notes and migration guidance: [docs/releases/v2.0.0.md](docs/releases/v2.0.0.md).

### Added

- Run-scoped, minimal evaluation snapshots under `data/eval_runs/`.
- Evaluation result schema validation, including score ranges and weighted-total checks.
- Cross-process table locking, atomic JSON replacement, record versions, and evaluation-input hashes.
- Conflict detection, safe rebasing, partial commits, idempotent retries, and automatic run cleanup.
- Eight deterministic tests for concurrency, stale results, validation, corruption handling, and lifecycle behavior.
- GitHub Actions CI on Python 3.10 for Ubuntu and Windows.

### Changed

- Search and evaluation computation may overlap, while all canonical-table commits remain serialized.
- `data/jobs_table.json` remains the only canonical job table; evaluation workers no longer write shared state directly.
- `merge` now returns an `eval_run` manifest containing the run ID and task path.
- `update` now requires `--run-id` and accepts only evaluation-owned fields associated with that run.
- Corrupt canonical JSON now fails closed instead of being treated as an empty table.

### Security

- Evaluation manifests exclude the raw CV and avoid copying the complete canonical job table.
- Search-owned fields cannot be overwritten by evaluation output.
- Out-of-range scores and malformed evaluation results are rejected before persistence.

### Breaking

- Direct callers of `scripts/merge_jobs.py update` must pass the `run_id` returned by the corresponding `merge` call.
- Evaluation workers must echo `dedup_key`, `base_record_version`, and `jd_input_hash` from their assigned task.
- Legacy evaluation results without run-scoped snapshot metadata are rejected.

### Verification

- Local: 8/8 tests passed in 1.38 seconds; Ruff and Python compilation passed.
- GitHub Actions: 8/8 tests passed on Ubuntu in 2.12 seconds and Windows in 0.95 seconds.
- Main CI run: [30623328782](https://github.com/sangowu/job-matcher-skill/actions/runs/30623328782).

[Unreleased]: https://github.com/sangowu/job-matcher-skill/compare/v2.2.0...HEAD
[2.2.0]: https://github.com/sangowu/job-matcher-skill/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/sangowu/job-matcher-skill/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/sangowu/job-matcher-skill/compare/v1.0.0...aefbdf9816a0ff17f246eb3c4b501cffa3e51c25
