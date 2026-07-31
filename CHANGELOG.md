# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[2.0.0]: https://github.com/sangowu/job-matcher-skill/compare/v1.0.0...aefbdf9816a0ff17f246eb3c4b501cffa3e51c25
