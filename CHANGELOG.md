# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `board_harvest.py` now recovers a board from a company's own careers URL, where the board token is not present to be extracted. An embedded ATS board puts only the provider and a job id in the URL (`gh_jid` / `ashby_jid`), so `extract_board_hint()` guesses candidate tokens from the hostname and the observed job id is what settles it: a guessed board is registered only when that job id is actually among the jobs it returns. A board answering is not evidence -- a hostname guess can land on a different company's board on the same ATS, which happened once during the catalog build. Guesses cost one request each and are bounded by their own `--hint-limit` (default 3), separate from the direct-board `--limit`. The job id is used for that check and discarded; only provider, token, and counts reach the registry.
- The cookie consent classifier now takes `container.visible` and returns `proceed` for a dialog that is not displayed. Sites routinely leave an empty, invisible `role=dialog` shell in the DOM after consent was already given; classifying that shell found zero safe actions and paused the run over a banner no human could see. Visibility only decides whether to stop -- a displayed shell still pauses, `ask_every_time` still wins, and no control is ever selected because of it.
- The ATS round budget raised in config exceeded `ats_pipeline`'s own ceiling, so the entire structured channel failed closed with `ats_boards_per_round must be between 1 and 10` before reaching a single board. The ceilings now sit at 30 boards and 100 requests, and a new config contract test asserts the shipped `config.json` is accepted by every script that reads it, plus that one market's board list fits inside a single round.
- A careers portal that redirects to its own public ATS board is now a board handoff instead of a dead end. Browser tasks carry `ats_handoff_hosts` and `on_ats_handoff`, `extract_board()` recognises board root pages as well as job URLs, and the redirect target goes straight to `board_harvest.py` -- so the employer is fetched in one structured request next round rather than browsed expensively or skipped as `host_boundary`. The 2026-09-22 trial lost a public-sector source exactly this way.
- `browser_control.py action`: a provider-neutral path for a local browser to report what the Agent executed. BrowserOS Neo and an authorized user browser are driven by the Agent's own tools, so this process never saw their actions and every Neo-backed run finished with `metrics_status=incomplete`, `missing_operations=browser`, and a report health of `unknown`. Reported actions now satisfy the browser completeness gate; the path needs no credentials or session budget, and only successful actions count.
- `seed_promotion.py`: harvested boards accumulate in `data/source_registry.json`, which is gitignored along with the rest of `data/`, so the growth never left one machine. Promotion appends verified, unexpired, URL-rebuildable sources to `references/source_seeds.json`, keeps `markets.json` in step, and re-origins the local record to `seed` so the next `merge_seeds()` does not reject its own catalog.
- `board_harvest.py`: an Ashby/Greenhouse/Lever job URL already carries its company's board identity, so one observed candidate now teaches the registry an entire employer. Each recovered board is probed once through the existing read-only adapter before it is trusted, its markets come from the locations seen in that probe, and only provider, token, markets, and counts are written -- never the URL, job title, JD, or CV.
- A verified public ATS board catalog: 18 Ashby/Greenhouse boards covering Ireland, the UK, and Germany, each confirmed by a read-only board fetch whose observed job locations determined its markets. Counts and rejection reasons are in [docs/ats-source-catalog.md](docs/ats-source-catalog.md).
- Deterministic cross-channel discovery waves: the plan owns remaining-task availability, the Agent executes only the current wave, and `discovery_batch.py` exposes a next wave only after canonical merge yield checks pass.
- Count-only live evidence for a BrowserOS Neo plus Web Search first wave, including semantic platform search, consent pauses, local attention state, open-web contribution, and fail-closed suppression of the next wave.
- Count-only evidence for a completed three-wave BrowserOS Neo plus Web Search run: 12 terminal task results, two explicit skips, three single-writer batch commits, one suppressed duplicate, two unique strong-identity candidates, and a `plan_exhausted` stop in an automatically cleaned temporary store.
- Count-only evidence for a full real-CV production run through three discovery waves, canonical merge, two JD evaluations, and a two-job HTML report, including explicit site compatibility outcomes and the current local-browser runtime-metric gap.
- A fail-closed, multilingual accessibility classifier and HTML setting for `necessary_only` versus `ask_every_time`; it never reads Cookie storage or selects accept-all/granular consent controls.
- Traditional-Chinese and generic English exact-label coverage for necessary-only consent, added from live consent banners without introducing site-specific selectors.

- A read-only `DiscoveryPlan` compiler that joins localized market queries, the URL-free source-health plan, and the public source catalog into bounded browser, Web Search, and optional structured-source tasks without hard-coded site selectors.
- Diversity-first browser source selection, source-policy enforcement, public Web source hints, and explicit semantic-accessibility/auth/privacy constraints for each browser task.
- A restart-safe `discovery_batch.py` handoff that requires one terminal result per current-wave task, validates CandidateEnvelope provenance against its task, commits all wave channels through one canonical merge, retries source-state failures without repeating merge, and returns a count-only continuation decision.
- Read-only source-batch preview validation so malformed source proposals/events fail before candidate persistence.
- A read-only discovery-mode selector with explicit `coverage`/`auto`/`model_only`/`browser_only`/`combined` routing, single-route Neo-first legacy `auto` fallback, and deterministic connection-loss versus human-action behavior.
- A provider-neutral local-browser capability probe and runtime adapter contract for BrowserOS Neo or an authorized user browser, with dedicated-tab privacy rules and canonical CandidateEnvelope handoff through the existing merge writer.
- A loopback-only local-browser control panel with persisted non-sensitive discovery mode, polling status UI, flashing browser-tab/beacon alerts, CSRF-protected resume requests, and a low-cardinality event CLI for login, verification, consent, and rate-limit pauses.
- Count-only Phase 4 runtime evidence for successful BrowserOS Neo MCP and authorized Chrome fallback paths, plus a real LinkedIn login pause/panel-resume/authenticated-session/same-tab-search handoff; the full single-market CV production path is now verified, while pagination, broad site compatibility, CAPTCHA/ambiguous-consent recovery, rate-limit recovery, and local-browser metric completeness remain live gates.
- Versioned `ie`/`uk`/`cn`/`de` market metadata, English/German/Simplified-Chinese role taxonomy, and a deterministic opt-in market planner with city/country/remote-scope normalization.
- Synthetic ten-observation-per-market offline fixtures and fixed strong-identity/routing ground truth for the four initial markets.
- A validated public source seed catalog with three local entry points and ten global company portals per initial market.
- A PII-safe, single-writer source registry with deterministic eligibility planning, TTL revalidation, idempotent proposal/event batches, and atomic legacy ATS import/rollback.
- A strict JSON/Python `CandidateEnvelope` contract and an opt-in dual-route handoff that serializes regional-registry and Agent Web Search candidates through the existing canonical merge writer.
- Batch-idempotent canonical merges, additive legacy provenance normalization, route/source/market discovery metrics, and retry manifests for merge-before-registry interruption recovery.
- Offline Phase D1 report evidence: market/source/verification filters, per-observation provenance, multi-source badges, market coverage cards, and explicit failed/skipped/not-collected/unknown empty states.
- An explicit Phase D2 four-market public-source smoke harness with fixed request/body/timeout/redirect limits, policy-enforced China skip behavior, count-only evidence, and a bounded 2026-09-18 run artifact.
- A strict count-only Phase E shadow-run contract, idempotent atomic evidence ledger, live-evidence-aware per-market rollout gate, and explicit off/shadow/opt-in/default market configuration that leaves every market off by default.
- A read-only Phase E shadow comparator that joins shared strong identities in memory, attributes regional/Web incremental contribution deterministically, computes JD/link and potential Top-N counts, and emits no candidate identities or business content.

### Changed

- Discovery budget now follows measured cost: the structured and Web Search channels own wave 1, while the browser is deferred by the new `browser_first_wave` (default 2) and narrowed to two sources per market per wave. The existing wave gate then withholds browser tasks entirely when the cheap channels already produced enough. `browser_first_wave: 1` restores the previous all-channels-at-once behaviour.
- `ats_boards_per_round` rises from 10 to 30 and `ats_requests_per_round` from 30 to 100; a measured board costs roughly one request and a fraction of a second, so the old caps bound the cheapest channel far tighter than its cost warranted.
- Source proposals accept `board_token` and `instance`, under the same rules as seeded boards: an `ats_board` proposal without a token, or one naming a provider with no adapter, is rejected.
- The repository default is now `ats_enabled: true`. The public ATS path is the cheapest discovery channel measured so far (one request per board, roughly 0.2-0.6s, job descriptions included) and stays bounded by its own request/page/concurrency caps.
- Structured discovery tasks now carry `provider`, `board_token`, and Lever's `instance`, so a board is fetched by provider identity instead of its human-facing `entry_url`.
- The repository default discovery mode is now `coverage`: model/Web Search runs alongside the preferred available browser provider (BrowserOS Neo, then an authorized user browser). Legacy `auto` retains its single-route compatibility behavior.
- Report language and market search languages are separate in the new planning boundary; `zh` and `zh-CN` normalize to internal `zh-Hans`, while the existing single-region Web Search path remains available.
- `markets.json` now references stable Phase B source IDs, while `multi_region_enabled: false` continues to prevent the new source plan from changing the production discovery path.
- Once the generic registry is initialized, the existing ATS pipeline reads and writes its ATS-board view there; without it, the legacy `ats_companies.json` path remains the compatibility fallback.
- Runtime metrics schema v6 adds low-cardinality Phase C discovery counts and merge provenance/idempotency fields without recording queries, titles, companies, URLs, CVs, or JDs.
- Legacy candidate arrays remain accepted by `merge_jobs.py`; strict discovery validation is activated by the Phase C `discovery_route` boundary, while `candidate_handoff.py` always validates the full envelope.
- Browser-discovered candidates reuse the canonical table and now preserve `browseros_neo` or `user_browser` provenance in the bilingual HTML report.
- Candidate provenance distinguishes `global_job_board` from market-local job boards, with bilingual report labels and scoped-read guidance that excludes account navigation metadata.
- A count-only live candidate smoke validates one scoped BrowserOS Neo extraction and active detail page through CandidateEnvelope and the real merge writer in an automatically cleaned temporary store; the initial unscoped-read privacy regression is retained as failed evidence.
- A bounded three-candidate same-page BrowserOS Neo batch preserves strong identities, browser route, and global-job-board provenance 3/3 through the temporary merge while keeping uncertain detail pages at `unknown`.
- Multi-region run metadata is additive: old `{profile_summary,new_count,cached_count,lang}` files still render, while new metadata can provide target markets, search/report languages, run time, and count-only route summaries.

### Fixed

- A job on a company's own careers page now shares its identity with the same job on the ATS vendor's host. A company that embeds its board serves the posting from its own domain with the provider job id in a query parameter and no vendor hostname anywhere in the URL, so `stripe.com/...?gh_jid=123` and `boards.greenhouse.io/stripe/jobs/123` were two records and two evaluations whenever the two listings phrased the title differently. `gh_jid` and `ashby_jid` now yield `greenhouse:<id>` and `ashby:<uuid>`; both parameter names are vendor-specific and their value formats match each provider's own job ids, so neither needs the host check `jk` requires. Only conventions that exist are recognised -- GitHub code search finds `gh_jid` 51712 times and `ashby_jid` 948, against 6 and 1 for `lever_jid` and `smartrecruiters_jid`. Stored records adopt the key on the next merge through `_ensure_job_identity()`, keeping their `record_id` so cached evaluations survive.
- Two different jobs on one company careers page no longer collapse into one record. `canonicalize_url()` keeps a short allowlist of job-id query parameters and drops the rest, so a careers page holding its job id in any other parameter -- `ashby_jid`, among others -- gives every job on that page the same host+path `url_key`. Both merge paths treated that key as an identity and merged on it alone, which did not produce a duplicate: the second job was absorbed into the first and lost, title and URL and all, in the same batch or against a record stored in an earlier round. A weak `url_key` now has to agree on company and title as well; a provider job id is still an identity on its own. Refusals are reported as `weak_url_key_collisions_prevented`.
- Every local store now shares one exclusive file lock (`scripts/_filelock.py`) instead of five hand-rolled near-copies with three defects between them. Windows reports `PermissionError`, not `FileExistsError`, while a holder's `unlink()` is still pending, and `exists()` reports `False` in that same window: measured over a 3-second, 5-thread handoff, 711 of them. `merge_jobs`, `source_registry`, and `runtime_metrics` read that pair as proof the lock was unreachable and failed the caller outright -- an aborted merge, an aborted registry write, a metric dropped with nothing to show for it; `shadow_gate` and `browser_control` did not catch `PermissionError` at all and let it escape. A real access failure is now told apart from a handoff by outlasting the timeout rather than by one observation, and the deadline is checked on every retry path, including the two that previously retried without consulting it.
- The lock's 50 ms poll capped handoff throughput at ~20/s however short the critical section was, so twenty contending writers spent 970 ms of a 2 s budget asleep and `test_twenty_concurrent_writers_preserve_parseable_table` flaked about once per full-suite run. At a 5 ms poll the same twenty writers finish in 0.17 s with a worst wait of 163 ms, taking the margin against the timeout from 2x to 12x.
- `shadow_gate`'s ledger lock had no stale handling at all, so a lock left behind by a killed process wedged the ledger until someone deleted the file by hand. It now reclaims a lock older than `STALE_LOCK_SECONDS`, matching every other store.
- `board_harvest.py` validated a custom seed catalog against the default `markets.json` rather than the one it was given, so a promoted catalog would fail its own consistency check on the next harvest.
- Seeded ATS boards reach the pipeline with their identity intact: seed-to-registry conversion dropped `board_token`, so every `ats_board` seed became a tokenless record that `ats_view_from_registry()` silently skipped, leaving the structured channel empty no matter how the catalog was curated.
- Seed validation now rejects an `ats_board` without a `board_token`, and rejects `ats_public_api` access for a provider that has no adapter, instead of planning a structured task that can never be fetched.
- Teamtailor job URLs now produce provider-owned `teamtailor:<job-id>` strong identities, allowing valid Web Search candidates to pass the shared CandidateEnvelope and canonical deduplication boundary.
- Phase E no longer treats missing real-CV Top-N baselines or repeated zero-useful-candidate runs as default-enable evidence; v2 count-only shadow records carry explicit baseline status while historical v1 ledger hashes remain readable.
- The legacy-table migration regression now uses a fixed clock, preventing its fixture from becoming stale as the real 30-day archive TTL advances.

## [2.4.0] - 2026-08-30

Detailed release notes: [docs/releases/v2.4.0.md](docs/releases/v2.4.0.md).

### Added

- A non-blocking startup version check that compares the local semantic version and Git revision with GitHub `main`, caches public metadata for 24 hours, and never auto-updates local files.
- Runtime metrics schema v5 with one PII-safe pipeline `run_id`, `run_start`/`run_finish` lifecycle events, Web Search page metrics, optional subagent token/cost fields, and explicit completeness reporting.
- A run-scoped metrics contract that pre-registers privacy rules and A/B quality/cost/latency gates before any further optimization claim.
- A bounded, count-only interleaved ATS HTTP-compression A/B harness with content-equivalence, request-count, job-count, and minimum wire-reduction gates.
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

- Runtime health is now `unknown` rather than `healthy` when a finished run is missing required events or an unfinished run exceeds the configured age limit; unavailable token/cost values remain `null` instead of being counted as zero.
- Public ATS requests now advertise gzip support by default while independently bounding compressed wire data and decompressed JSON; the transport switch remains injectable for controlled A/B tests.
- ATS HTML descriptions are converted to plain text, script/style content is discarded, and each handoff is capped at 50,000 characters. Evaluation workers treat it as untrusted data and use browser fetching only when no ATS text is available.
- Canonical jobs persist only `jd_content_hash`; a changed hash invalidates cached JD analysis and match scores. Completed/conflicted tasks immediately discard transient JD text and completed/expired run snapshots are deleted.
- The public ATS benchmark now reuses the production parser and pagination implementation so benchmark contracts cannot drift from runtime behavior; ATS remains explicitly disabled by default.
- Greenhouse discovery now recognizes EU public job-board pages while continuing to use the documented global API endpoint; oversized `content=true` responses may retry once without content inside the existing request budget, with response bytes and fallback counts recorded.
- Exact Provider IDs and canonical URLs now match before weak company/title matching. Disjoint strong IDs never merge by weak key alone; weak-only matches require compatible locations and exactly one target.
- Evaluation workers now echo `record_id`; legacy results without it remain accepted only when their `dedup_key` identifies exactly one task and job.

### Fixed

- Runtime metadata now reuses the Python 3.10-compatible version reader instead of importing Python 3.11-only `tomllib`, restoring the declared Python 3.10 CI path.
- Greenhouse entity-escaped HTML is decoded before plain-text extraction, preventing literal tags from entering evaluation input; Lever normalization now includes documented `lists` and `additionalPlain` sections instead of dropping requirements and closing content.
- ATS title prefiltering no longer accepts mobile, Android, iOS, or UI roles solely because an `AI` product suffix overlaps a preferred AI role. Standalone `ai` is low-information while explicit `AI evaluation`, `AI systems`, and `agent systems` phrases remain eligible.

### Security

- Compressed ATS payloads are decompressed through a bounded stream, preventing a small wire response from bypassing the existing 25 MB response limit.
- Raw ATS JD text is excluded from the canonical table, command output, runtime metrics, sync state, benchmark reports, and evaluation history; it exists only in an active local evaluation task.
- The ATS benchmark only performs allowlisted public GET requests and never persists job descriptions, titles, URLs, candidate data, API keys, or arbitrary exception text.
- The controlled ATS A/B artifact is count-only and omits CV/profile fields, board tokens, company names, job titles, URLs, API keys, and raw exception text.

### Performance

- A three-pair live A/B across one Ashby, Greenhouse, and Lever board kept 292 jobs, 292 JD-bearing jobs, and 3 requests identical in every arm. Median wire bytes fell from 4,581,392 to 948,090 (-79.31%); median wall time moved from 2,407.832 ms to 2,278.456 ms (-5.37%). Content fingerprints matched in all pairs. The byte reduction is the accepted optimization; public-network variance means the latency observation is not a guaranteed speedup.
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
