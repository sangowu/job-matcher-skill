# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A bounded three-provider ATS quality collector and count-only audit with pre-registered JD coverage, false-positive, direct/adjacent calibration, contract, and browser-fallback gates.
- ATS-to-evaluation JD handoff: normalized Ashby/Greenhouse/Lever descriptions now flow through run-scoped task snapshots so eligible workers can skip a second page fetch.
- `ats_handoff.py`, an orchestration entry point that keeps full ATS candidates in process/subprocess stdin while returning only count summaries and merge task metadata to the agent.
- Count-only JD availability, truncation, handoff, and handoff-character metrics, plus a reproducible three-provider Fake handoff benchmark.
- Opt-in production ATS enhancement pipeline for public Ashby, Greenhouse, and global/EU Lever boards, with a persistent board registry, deterministic CV-aware prefilter, independent request/page/concurrency budgets, partial-success handling, and merge-ready strong identities.
- Shared production/Fake ATS Provider contract and offline regressions covering single-response boards, sequential Lever pagination, EU routing, unlisted Ashby jobs, global request exhaustion, 404/429/timeouts, disabled routing, and unavailable-board transitions.
- PII-safe ATS sync state and runtime schema v4 metrics, plus a fixed three-provider Fake benchmark integrated into the release performance harness.
- A PII-safe controlled Web-only versus Web+ATS discovery-to-merge benchmark with isolated canonical tables, fixed local inputs, production hard-cap enforcement, and Web-result preservation checks.
- ATS Phase 1 architecture, official public-API contract notes, a bounded six-board benchmark, and PII-safe raw/summary evidence for Ashby, Greenhouse, and Lever.
- Regression tests for provider normalization, Ashby unlisted filtering, Lever sequential pagination/caps, failure metrics, output privacy, and weak identity collision reporting.
- Stable `record_id` and Provider-owned `identity_keys` for the canonical job table and evaluation snapshots, with lazy in-place migration for legacy tables.
- PII-safe identity migration/conflict metrics in runtime schema v3 and a fixed cold-core performance comparison.

### Changed

- ATS HTML descriptions are converted to plain text, script/style content is discarded, and each handoff is capped at 50,000 characters. Evaluation workers treat it as untrusted data and use browser fetching only when no ATS text is available.
- Canonical jobs persist only `jd_content_hash`; a changed hash invalidates cached JD analysis and match scores. Completed/conflicted tasks immediately discard transient JD text and completed/expired run snapshots are deleted.
- The public ATS benchmark now reuses the production parser and pagination implementation so benchmark contracts cannot drift from runtime behavior; ATS remains explicitly disabled by default.
- Greenhouse discovery now recognizes EU public job-board pages while continuing to use the documented global API endpoint; oversized `content=true` responses may retry once without content inside the existing request budget, with response bytes and fallback counts recorded.
- Exact Provider IDs and canonical URLs now match before weak company/title matching. Disjoint strong IDs never merge by weak key alone; weak-only matches require compatible locations and exactly one target.
- Evaluation workers now echo `record_id`; legacy results without it remain accepted only when their `dedup_key` identifies exactly one task and job.

### Fixed

- Greenhouse entity-escaped HTML is decoded before plain-text extraction, preventing literal tags from entering evaluation input; Lever normalization now includes documented `lists` and `additionalPlain` sections instead of dropping requirements and closing content.
- ATS title prefiltering no longer accepts mobile, Android, iOS, or UI roles solely because an `AI` product suffix overlaps a preferred AI role. Standalone `ai` is low-information while explicit `AI evaluation`, `AI systems`, and `agent systems` phrases remain eligible.

### Security

- Raw ATS JD text is excluded from the canonical table, command output, runtime metrics, sync state, benchmark reports, and evaluation history; it exists only in an active local evaluation task.
- The ATS benchmark only performs allowlisted public GET requests and never persists job descriptions, titles, URLs, candidate data, API keys, or arbitrary exception text.
- The controlled ATS A/B artifact is count-only and omits CV/profile fields, board tokens, company names, job titles, URLs, API keys, and raw exception text.

### Performance

- The Phase 5 bounded live sample used 3 public GETs across Ashby, Greenhouse, and Lever, normalized 291 jobs in 2,672.85 ms, and handed off complete JD text for 7/7 sampled jobs. All 7 five-dimensional results passed the contract; one direct job scored 92.0, five adjacent jobs averaged 65.15, one false positive produced a 14.29% false-positive rate, and no adjacent job was inflated to `strong_apply`. This is a small 3/3/1 provider sample with only one direct job, not a market-wide precision or browser A/B claim.
- The 30-iteration Phase 5 offline regression recorded core total p50/p95 of 53.541/60.395 ms and ATS Fake normalization p50/p95 of 5.133/5.536 ms. Both were slower than the Phase 4 snapshot, alongside slowdowns in unchanged core stages, so no performance improvement or parser-specific regression is claimed.
- The Phase 4 offline handoff baseline completed three-provider ATS normalization at p50 4.094 ms / p95 5.108 ms and normalization-through-evaluation-snapshot handoff at p50 7.408 ms / p95 8.367 ms across 30 runs. Each run handed off 3/3 JDs, made all 3 tasks eligible to skip page fetching, and persisted zero raw JDs in the canonical table. No live latency or scoring-quality claim is made from this fixture.
- A one-board live validation handed off JD text for 8/8 emitted candidates with one request; a purposive three-JD five-dimensional audit produced 2 `strong_apply` and 1 `apply`, with 3/3 contract updates accepted, zero conflicts/rejections, and zero raw JD text in completed tasks, the canonical table, or metrics. The sample is single-company and not a market-wide quality estimate.
- The three-provider offline ATS fixture completed 3 boards / 3 requests / 3 emitted jobs per iteration at p50 6.654 ms and p95 7.867 ms across 30 measured runs, with no external calls. The same machine run showed core total +22.6% p50 / +32.9% p95 versus the earlier checkpoint alongside similar slowdowns in unrelated harness sections; the observation is retained, causation is not claimed, and no core speedup is claimed.
- The production-adapter public regression completed 6/6 boards with 7 requests, 414 normalized jobs, no truncation/rate limiting, and zero strong-identity duplicates; its artifact is count-only and PII-safe.
- The controlled Phase 3 run preserved all 5 fixed Web records and produced 24 combined unique records: 19 incremental identities and 1 avoided duplicate evaluation. Three boards succeeded with 5 requests and 48,955,686 response bytes in 12,653.682 ms end-to-end. No candidate-quality or browser-fallback improvement is claimed because JD evaluation handoff was not measured.
- The initial title-level quality audit failed: the fixed Web control was 4/5 target-relevant, while ATS output was 5/20 target-relevant, 3/20 adjacent/stretch, and 12/20 false positives. After the filter fix, the final bounded replay emitted 8 candidates: 6 target-relevant, 2 adjacent/stretch, and no false positives (75% strict precision). All 8 links were alive, all 5 Web records were preserved, and 1 duplicate evaluation was still avoided; single-company concentration remains a coverage warning.

## [2.3.0] - 2026-08-25

Detailed release notes: [docs/releases/v2.3.0.md](docs/releases/v2.3.0.md).

### Added

- Optional Kernel BYOK remote-browser fallback with visual screenshot/mouse/keyboard controls, bounded listing pagination, Live View handoff, and a deterministic Fake Provider for CI.
- A one-shot setup page bound to `127.0.0.1`; provider keys are tested before being stored in the OS keychain, with `KERNEL_API_KEY` as the non-UI fallback.
- Atomic per-round admission limits: at most 2 concurrent browsers, 3 pages per site, 10 sessions, 10 minutes of handoff wait, and USD 1.00 estimated cost by default.
- Role-specific subagent profiles and PII-safe requested/effective model, reasoning-effort, latency, success, valid-item, and fallback metrics.
- PII-safe browser action/session/handoff/rate-limit/estimated-cost metrics and runtime schema v2, with v1 event compatibility.
- `scripts/benchmark_pipeline.py` for repeatable 15-job cold-core and 10-session Fake Provider release measurements, including raw runs and baseline deltas.
- `[project]` metadata in `pyproject.toml` with `version` as the single source of truth, read at runtime by `_jobutil.skill_version()` and reported by `summarize_metrics.py`.
- Tests that fail when `pyproject.toml`, the newest `CHANGELOG.md` release heading, and `docs/releases/vX.Y.Z.md` drift apart.
- Documentation drift tests: every script and every `config.json` knob must appear in both READMEs, every release note must be linked, and no knob may exist that nothing reads.

### Documentation

- `WORKFLOW.md` now distinguishes web-search result pagination from sequential job-site listing pagination and specifies model selection, metric recording, remote-browser fallback, budget admission, handoff, and cleanup.
- The confirmed architecture and staged ATS boundary are recorded in `docs/browser-provider-control-panel.md`; ATS integration remains a later independently validated phase.
- Both READMEs now cover `round_timer.py` and `cp_hash.py`, the `eval_run_stale_hours` and `consecutive_empty_stop` knobs, the multi-market search strategy, batch overlap, abandoned-snapshot recovery, and the untrusted-input boundary.
- `search_playbook.md` names `stop_threshold`, `max_websearch_calls`, and `consecutive_empty_stop` instead of hardcoding their values in prose.
- The fallback ladder in `WORKFLOW.md` and `scoring_rubric.md` now honors `enable_headless_fallback`, which previously existed in `config.json` but was referenced nowhere.
- `docs/releases/v2.2.0.md` records the post-release controlled measurement of batch overlap (measured 16.6–22.1% saving, within 0.3 pp of the model) and narrows the remaining limitation to live-latency variance.

### Removed

- `cv_cache` and `report_keep_history` from `config.json`. Neither was read by any script or referenced by any instruction document — CV profiles are always cached and reports always keep history — so they promised control that did not exist.

### Security

- API keys, cookies, session IDs, Live View URLs, page URLs, typed text, screenshots, CV/JD text, and arbitrary exception strings are excluded from metrics by strict field and category allowlists.
- Remote stealth is disabled by default; the workflow prohibits automatic CAPTCHA solving, login simulation, proxy rotation, or bypassing site controls.

### Fixed

- Real Kernel SDK 0.94.0 smoke testing found that `type_text` no longer accepts the legacy `smooth` keyword. The adapter now uses the current `type_text(id, text=...)` contract, and the optional dependency range records the tested `0.94.x` API family.
- The same smoke run found that the remote Linux host expects the X11 `Return` key symbol instead of the common agent spelling `Enter`. The adapter now normalizes `Enter`, `ENTER`, and combinations such as `Ctrl+Enter` before calling Kernel.

### Performance

- On the fixed 15-job cold benchmark, core total p50 changed from 55.741 ms to 60.666 ms (+8.8%) and p95 from 64.107 ms to 64.604 ms (+0.8%), with all 15 jobs merged, updated, and rendered in every run. This feature release does not claim a deterministic-core speedup; the small render overhead remains visible and documented.
- The new Fake Provider path completed 10 sessions / 70 recorded actions per iteration at p50 56.763 ms and p95 73.022 ms with 100% success. No external requests or provider charges were used.

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
