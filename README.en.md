# job-matcher

**English** | [中文](README.md)

Release documentation: [Changelog](CHANGELOG.md) · [v2.4.0 release notes](docs/releases/v2.4.0.md) · [v2.3.0 release notes](docs/releases/v2.3.0.md) · [v2.2.0 release notes](docs/releases/v2.2.0.md) · [v2.1.0 release notes](docs/releases/v2.1.0.md) · [v2.0.0 release notes](docs/releases/v2.0.0.md)

> An **agent skill (for Claude Code & Codex)**: give it your **CV + job intent**, and it extracts CV fields, discovers matching jobs through **model search plus BrowserOS Neo or an authorized user browser**, and generates an **interactive HTML report**.

A lightweight take on [JobRadar](https://github.com/sangowu/JobRadar) — it combines model/Web search with an Agent-connected BrowserOS Neo or authorized user browser, with an optional BYOK isolated browser as a fetch fallback.

---

## ✨ Features

- 📄 **CV parsing**: PDF / DOCX / TXT / MD, or pasted text (no OCR).
- 🧠 **Structured extraction**: target roles, skills, seniority, locations, languages; auto-leveling by *relevant* years.
- 🔎 **Live job retrieval**: runs browser-platform and model/Web discovery together by default; the browser provider follows Neo → authorized user browser, while a regional source catalog selects platforms without hard-coded site selectors.
- 🎯 **5-dimension scoring**: title / seniority / skills / location / must-have, with a five-tier recommendation (strong apply → skip). Deterministic seniority caps and contract validation catch scoring drift.
- 🗂️ **Incremental cache**: three-layer cache (CV / JD / match score); multi-source same-job aggregation with exact job-id matching on regional platforms; auto re-score when the query changes.
- 🛡️ **Untrusted input isolation**: search results and JD text are treated as data and their embedded instructions ignored; report JSON is escaped and links are restricted to http(s).
- 📊 **Interactive report**: two-column layout (job list 30% + detail 70%) + score badges + dark mode + sort/search + market/source/verification filters + multi-source discovery evidence + 7/30-day runtime health snapshots + zh/en i18n, a self-contained single-file HTML.
- 🌐 **Optional isolated browser**: Kernel BYOK as the final fetch fallback, with bounded listing pagination, visual controls, and Live View handoff; disabled by default, with a Fake Provider in CI.

## 🏗️ Architecture

- **Main agent = orchestrator**: runs scripts, fuses the query, asks the user, spawns subagents.
- **Subagents do the heavy-context work** (CV extraction / search / scoring): raw text stays inside subagents; the main context only carries "paths + small JSON".
- **Python scripts do the deterministic work**: parse, validate, dedup/aggregate/cache, verify, render.
- **Parallel compute, serialized commits**: batch N's evaluation runs alongside batch N+1's search, while `jobs_table.json` has one guarded write path with evaluation snapshots, a cross-process lock, and atomic replacement. Snapshots left pending too long are abandoned on the next merge, so jobs never stay stuck in evaluation.

```
CV + query
   │ [script] extract_cv          → plain text + cv_hash
   │ [cache check]                → hit → skip extraction
   │ [subagent] extract CVProfile → [script] validate_profile
   │ [main agent] fuse query      → search_plan + candidate_profile
   │ [discovery plan] browser platform + model/Web + optional ATS → [script] merge_jobs (dedup/cache/eval snapshot)
   │ [parallel subagents] coarse → fine (fetch JD) + scoring + liveness → [script] conditional commit
   │ [script] render_html         → report_*.html (auto-opened)
   ▼
interactive HTML report
```

**Fallback ladder** (shared by liveness check & JD fetch): `WebFetch → requests static fetch → local headless → optional isolated remote browser → mark "unverified" without blocking`.

## 📁 Structure

```
job-matcher/
├── SKILL.md              # trigger description + orchestration entry
├── WORKFLOW.md           # agent-neutral full workflow
├── config.json           # tunable knobs
├── docs/monitoring.md     # runtime metrics, thresholds, and health summary
├── references/           # instructions read on demand
│   ├── cv_schema.md          # CV extraction rules
│   ├── scoring_rubric.md     # 5-dim scoring + tier thresholds
│   ├── search_playbook.md    # fan-out / per-market / adaptive batching
│   ├── markets.json          # ie/uk/cn/de locations, languages, and query templates
│   ├── role_taxonomy.json    # stable English/German/Chinese role families
│   ├── source_seeds.json     # verified four-market public seeds (no credentials)
│   ├── multi_region_smoke_plan.json # Phase D2 fixed sources and request limits
│   ├── shadow_run.schema.json # Phase E count-only shadow evidence contract
│   ├── shadow_compare.schema.json # ephemeral Phase E comparison input
│   ├── candidate_envelope.schema.json # Phase C discovery candidate contract
│   ├── ats_phase1_boards.json # public-company sample for the ATS baseline
│   └── ats_phase5_quality_boards.json # three-provider quality sample
├── scripts/              # deterministic Python scripts
│   ├── extract_cv.py         # parse CV → text + hash
│   ├── validate_profile.py   # validate + seniority→levels mapping
│   ├── market_plan.py        # validate resources and build deterministic market plans
│   ├── discovery_mode.py     # select discovery routes and safe fallback from read-only capability status
│   ├── discovery_plan.py     # compile market/health/catalog inputs into multi-channel tasks
│   ├── discovery_batch.py    # validate all task results, merge once, and decide expansion
│   ├── local_browser_probe.py # validate the Agent-observed local-browser tool surface
│   ├── local_browser_panel.py # localhost settings, status, and flashing action panel
│   ├── cookie_consent.py    # accessibility consent classifier: necessary-only or pause
│   ├── browser_candidate_smoke.py # validate browser candidates in a temporary merge store
│   ├── source_registry.py    # seed validation, health state, migration, and source plans
│   ├── candidate_contract.py # strict Phase C CandidateEnvelope validation
│   ├── candidate_handoff.py  # serialize dual discovery routes into the canonical writers
│   ├── multi_region_smoke.py # explicit count-only four-market public-source smoke
│   ├── shadow_gate.py        # idempotent Phase E ledger and per-market gate
│   ├── shadow_compare.py     # read-only incremental/overlap/JD/Top-N comparison
│   ├── analysis_contract.py  # validate JDProfile/MatchScore worker output
│   ├── merge_jobs.py         # single writer: dedup/cache/eval snapshots/conditional commit
│   ├── runtime_metrics.py    # PII-safe JSONL events and health calculations
│   ├── search_metrics.py     # page-level Web Search counts and latency
│   ├── summarize_metrics.py  # 7/30-day Markdown/JSON health report
│   ├── round_timer.py        # full-round timing, compared per orchestration mode
│   ├── subagent_metrics.py   # requested/effective subagent model and effort metrics
│   ├── browser_provider.py   # Kernel/Fake providers and safe settings
│   ├── browser_control.py    # remote visual-browser CLI; metric self-reporting for local browsers
│   ├── browser_setup.py      # one-shot localhost setup page
│   ├── browser_workflow.py   # listing pagination/pause state machine
│   ├── ats_provider.py       # public Ashby/Greenhouse/Lever GET adapters and Fake
│   ├── ats_pipeline.py       # ATS registry, prefilter, sync, and normalization
│   ├── ats_handoff.py        # in-memory ATS JD handoff to canonical merge
│   ├── board_harvest.py      # recover ATS boards from job URLs and register verified ones
│   ├── seed_promotion.py    # promote verified harvested sources into the tracked seed catalog
│   ├── benchmark_pipeline.py # fixed small core/Fake Provider benchmark
│   ├── benchmark_ats.py      # bounded public ATS regression using production adapters
│   ├── benchmark_ats_e2e.py  # controlled fixed-Web vs Web+ATS A/B
│   ├── benchmark_ats_quality.py # three-provider JD/scoring quality audit
│   ├── benchmark_ats_compression.py # interleaved ATS HTTP compression A/B
│   ├── cp_hash.py            # stable candidate_profile hash
│   ├── verify_jobs.py        # dead-link / closed-posting detection
│   ├── fetch_rendered.py     # headless render fallback (reuses system browser)
│   ├── render_html.py        # render HTML report
│   ├── _jobutil.py           # shared: normalization / dedup keys / URL canonicalization
│   └── _filelock.py          # shared: cross-process exclusive file lock (every local store)
├── assets/template.html  # static report template (Tailwind + vanilla JS)
└── data/                 # runtime data (.gitignored, contains PII)
```

Multi-region Phase A/B/C remains an explicit entry point.
`python scripts/market_plan.py validate` checks the versioned market and role
resources; `python scripts/market_plan.py plan` accepts `{cv_profile,user_intent}`
on stdin and produces market, language, location, and Web-query plans for
Ireland, the UK, China, and Germany. It neither searches nor mutates the job
table. `python scripts/source_registry.py validate` checks public seeds; `init`
atomically merges them into `data/source_registry.json` and read-only imports a
legacy `data/ats_companies.json` when present; `plan --markets ie uk` returns
only enabled, verified, unexpired sources and emits a global source once. None
of these commands executes a search, so the existing single-region
Web Search/ATS/merge/report path stays compatible. In Phase C the orchestrator
may start `regional_registry` and `agent_web_search` together, wait for both
routes to report success, failure, or skip, then submit their strict
CandidateEnvelope batches under one `batch_id` to `candidate_handoff.py`. The
handoff commits jobs/evaluation tasks through the sole `merge_jobs.py` writer
before applying source-state updates idempotently. See
[`docs/multi-region-phase0-baseline.md`](docs/multi-region-phase0-baseline.md)
[`docs/source-registry-phase-b.md`](docs/source-registry-phase-b.md), and
[`docs/multi-region-phase-c.md`](docs/multi-region-phase-c.md).
The offline Phase D1 report contract is documented in
[`docs/multi-region-phase-d1.md`](docs/multi-region-phase-d1.md).
The bounded Phase D2 public-source smoke, privacy boundary, and observed evidence
are documented in [`docs/multi-region-phase-d2.md`](docs/multi-region-phase-d2.md).
It requires an explicit `--live` flag and is not part of default CI or production
discovery.
The count-only Phase E shadow contract, per-market thresholds, and current
ineligible state are documented in
[`docs/multi-region-phase-e.md`](docs/multi-region-phase-e.md).
`multi_region_enabled` defaults to `false`; regional source execution remains
opt-in and does not replace the existing single-region flow automatically.

Discovery mode Phases 1+2 provide deterministic
`discovery_mode.py plan|event` and `local_browser_probe.py probe` contracts.
Default `coverage` runs model/Web Search alongside one authorized browser
provider, selected BrowserOS Neo first and then an authorized user browser.
Legacy `auto` retains its single-route behavior, and `browser_only` never silently
switches to model search. `discovery_plan.py` then joins the market plan, URL-free
health plan, and public source catalog into bounded browser/Web/structured tasks.
Detection uses tools already exposed to the Agent; it does not scan
ports, browser configuration, cookies, or existing tabs. Browser observations use
a dedicated tab and enter the same CandidateEnvelope/merge table. See
[`docs/discovery-mode-phase1.md`](docs/discovery-mode-phase1.md) and
[`docs/local-browser-phase2.md`](docs/local-browser-phase2.md). A selected browser
route may run `python scripts/local_browser_panel.py serve` for a loopback control
panel. It stores only mode and low-cardinality state, then flashes for login,
verification, consent, or rate limiting until the user requests resume. See
[`docs/local-browser-phase3-panel.md`](docs/local-browser-phase3-panel.md). The existing
Kernel remote browser remains a separate optional fetching fallback, not a way
to reuse local login sessions.

DiscoveryPlan assigns cross-channel tasks to deterministic waves; the Agent
executes only `initial_wave_id` first. After the current wave finishes,
`discovery_batch.py` requires exactly one terminal result per wave task,
validates candidate route/source/market/language, and sends all wave channels
through one canonical merge. Its content-free manifest supports safe retries.
New-candidate yield, plan-owned remaining waves, and stop thresholds determine
whether it exposes `next_wave_id`; callers cannot override that decision. This
does not claim complete JDs or qualified CV matches.
Bounded runtime smokes on 2026-09-21 separately verified dedicated-tab
create/navigate/read/close through the BrowserOS Neo MCP primary path and the
user Chrome fallback. Neo remains the preferred browser provider when available.
A LinkedIn smoke also verified login pause, panel attention/resume,
authenticated-session reuse, and a bounded same-tab search. Live candidate
smoke then passed scoped main-content extraction, detail liveness,
CandidateEnvelope validation, and a temporary-store merge; a three-candidate
same-page batch also retained provenance 3/3. On 2026-09-22, a real-CV,
Dublin-first Ireland production run completed all three waves: 12 tasks reached
terminal outcomes and two strong-identity candidates passed the production
merge, JD evaluation, and HTML report. The run also showed that direct Neo MCP
actions do not yet emit repository browser metrics, so report health correctly
remains `unknown`. Host aliases, ambiguous consent, custom comboboxes,
additional-page pagination, CAPTCHA recovery, and rate-limit recovery remain
live gates. See
[`docs/local-browser-phase4-smoke.md`](docs/local-browser-phase4-smoke.md).
The deterministic first-wave Neo + Web Search smoke, consent pauses, and proof
that the next wave was not dispatched are in
[`docs/discovery-wave-live-smoke.md`](docs/discovery-wave-live-smoke.md). The
full production trial is documented in
[`docs/browseros-neo-production-trial-2026-09-22.md`](docs/browseros-neo-production-trial-2026-09-22.md).

## 🚀 Usage

Clone into your skills dir (use folder name `job-matcher` to match the skill name):

```bash
# Claude Code
git clone https://github.com/sangowu/job-matcher-skill ~/.claude/skills/job-matcher
# Codex
git clone https://github.com/sangowu/job-matcher-skill ~/.agents/skills/job-matcher
```

Both auto-discover it. Then in chat:

> Here's my CV `D:\cv.pdf`, find me remote backend roles

Or paste your CV text + job intent. The skill runs the full pipeline and opens the report in your browser.

## ⚙️ Configuration

`config.json` centralizes all knobs (tune to taste):

| Key | Default | Description |
|-----|---------|-------------|
| `top_n` | 15 | jobs shown in the final report |
| `precise_buffer` | 5 | extra jobs fetched for fine ranking |
| `version_check_enabled` | true | check whether the local skill matches GitHub `main` at startup |
| `version_check_interval_hours` | 24 | GitHub version-check cache duration; no network call inside the TTL |
| `version_check_timeout_seconds` | 3 | timeout in seconds for one read-only GitHub request |
| `max_parallel_subagents` | 3 | per-batch parallelism cap |
| `subagent_profiles` | see config | requested model, reasoning effort, and context isolation per role |
| `max_websearch_calls` | 6 | total web-search call cap |
| `discovery_mode` | coverage | `coverage` runs browser + model search; legacy `auto` keeps one route; `model_only`, `browser_only`, and `combined` remain available |
| `cookie_consent_policy` | necessary_only | automatically reject optional cookies only through one unambiguous semantic button; `ask_every_time` is also available |
| `discovery_max_waves` | 3 | deterministic discovery-wave cap per plan |
| `browser_sources_per_market` | 2 | per-market, per-wave browser-source cap, with source-type diversity in the browser's own first wave |
| `browser_first_wave` | 2 | wave the browser starts in; structured/Web Search run first so the browser stays a fallback |
| `browser_queries_per_source` | 2 | localized query cap per browser source and run |
| `web_queries_per_market_per_wave` | 2 | per-market Web Search query cap in each wave |
| `web_source_hints_per_task` | 6 | public source-hint cap per Web Search task |
| `multi_region_enabled` | false | master multi-region source flag; all effective market modes are off while false |
| `multi_region_rollout` | all four off | independent off / shadow / opt_in / default mode per market; default requires the Phase E gate |
| `stop_threshold` | 12 | stop once enough net-valid jobs found |
| `consecutive_empty_stop` | 2 | stop after N consecutive empty batches |
| `ats_enabled` | false | enable the public ATS enhancement pipeline; explicitly off by default |
| `ats_max_concurrency` | 3 | hard cap for concurrent ATS boards |
| `ats_boards_per_round` | 30 | hard cap for boards synced per round (pipeline ceiling 30) |
| `ats_requests_per_round` | 100 | hard cap for ATS HTTP requests per round |
| `ats_page_size` | 50 | Lever page size |
| `ats_max_pages` | 10 | hard cap for sequential pages per Lever board |
| `ats_timeout_seconds` | 30 | timeout in seconds for one public ATS GET |
| `ats_registry_ttl_days` | 30 | interval before a verified board is due again |
| `jd_ttl_days` | 30 | JD cache validity |
| `seniority_mode` | balanced | strict / balanced / stretch |
| `enable_headless_fallback` | true | headless fallback switch |
| `headless_budget` | 3 | headless calls per run |
| `remote_browser_enabled` | false | enable the isolated remote browser as the final fallback |
| `browser_provider` | kernel | `kernel`; `fake` is test-only |
| `browser_max_concurrency` | 2 | hard cap for concurrent remote browsers |
| `browser_max_pages` | 3 | hard cap for sequential pages per job listing |
| `browser_session_budget` | 10 | hard cap for new remote sessions per round |
| `browser_cost_limit_usd` | 1.0 | estimated per-round cost hard cap in USD |
| `browser_handoff_timeout_minutes` | 10 | human-handoff hard timeout in minutes |
| `browser_allow_handoff` | true | allow a temporary Live View URL for user action |
| `browser_timeout_seconds` | 600 | hard timeout for one remote session |
| `browser_headless` | false | hide provider browser UI; off to preserve handoff |
| `browser_stealth` | false | stealth switch; off and never used to bypass verification |
| `table_lock_timeout_seconds` | 10 | maximum wait for the canonical-table write lock |
| `stale_lock_seconds` | 120 | age at which an abandoned lock may be reclaimed |
| `eval_run_stale_hours` | 2 | age at which an unfinished evaluation snapshot is abandoned |
| `monitoring_default_window_days` | 7 | default health-report window |
| `monitoring_thresholds` | see config | conflict, rejection, success, lock-wait, and backlog limits |

`python scripts/version_check.py` compares the local `pyproject.toml` version and Git commit with GitHub `main`, caching the result in ignored `data/version_check.json`. It reads or uploads no CV, JD, or search data, requires no GitHub token, and never updates files automatically. Offline, timeout, and rate-limit failures return `unknown` without blocking the job pipeline. Use `python scripts/version_check.py --force` only for an immediate read-only diagnostic.

Runtime state has one canonical table, `data/jobs_table.json`. `record_id` is the stable record/evaluation primary key and `identity_keys` retain platform job ids; company + title `dedup_key` is only a compatibility weak key. Disjoint strong ids never merge solely because company and title match, while weak matching also requires compatible locations and a unique target. Legacy tables gain the identity fields in place on the next merge/update. Each evaluation batch gets a minimal `data/eval_runs/<run_id>.json` snapshot. When ATS already returned a JD, its text exists only in that task snapshot, the canonical table stores only a content hash, and the worker can skip page fetching. A completed/conflicted task drops its text immediately; the completed run is released after a PII-free summary is appended to `history.jsonl`. Every merge/update also appends a PII-safe event to `data/metrics.jsonl`.

The remote browser is optional. After installing the extra, launch the one-shot setup page. It binds only to `127.0.0.1`; after a successful connection test the key goes to the OS keychain, while non-secret settings go to ignored `data/browser_provider.json`:

```text
python -m pip install "kernel>=0.94,<1" keyring
python scripts/browser_setup.py
python scripts/browser_control.py test
```

Headless environments may use `KERNEL_API_KEY`. The controller exposes `create/screenshot/click/type/press/scroll/close` to the browser subagent. Live View URLs are returned only ephemerally and never stored in metrics or files.

## 📈 Runtime monitoring

```text
python scripts/summarize_metrics.py --days 7 --format markdown
python scripts/summarize_metrics.py --days 30 --format json
python scripts/summarize_metrics.py --fail-on-breach
```

The report covers run completeness, Web Search calls/effective candidates, throughput/cache behavior, evaluation rates, effective subagent model/effort/token/cost coverage, browser and ATS counts, latency percentiles, and backlog state. Threshold violations produce `degraded`; missing required events produce `unknown` rather than a false `healthy`. `--fail-on-breach` exits with code 2 for either case. See [the monitoring guide](docs/monitoring.md) and the [run-scoped metrics contract](docs/run-metrics-contract.md).

Full-round wall clock is collected separately, because per-script duration is a rounding error next to the search and evaluation work between calls and cannot answer whether overlapped batching pays off:

```text
python scripts/round_timer.py start          # -> {"run_id": "round-...", "round_id": "round-..."}
python scripts/search_metrics.py --ok --run-id <R> --query-slot q1 --duration-ms 120
python scripts/round_timer.py finish --round-id <R> --orchestration overlapped|serial --expect subagent
```

The summary reports p50/p95 per mode plus `overlap_saving_pct`, which stays `n/a` until both modes have samples.

`monitoring_thresholds.unfinished_run_age_minutes_max` controls when an unfinished run becomes stale and changes health to `unknown`; the default is 120 minutes.

Release regressions use a fixed 15-job cold dataset and 10 Fake sessions: `python scripts/benchmark_pipeline.py --output <json> --baseline docs/performance/v2.2.0-small-baseline.json`. The artifact contains raw iterations, p50/p95, absolute and relative changes, with no real web search or cloud-provider calls; it also verifies that all three Fake ATS JDs reach temporary tasks while the canonical table contains zero raw JDs. See [`docs/performance/strong-job-identity-baseline.md`](docs/performance/strong-job-identity-baseline.md) for the identity-migration run, [`docs/performance/ats-phase2-fake-baseline.md`](docs/performance/ats-phase2-fake-baseline.md) for the three-provider offline ATS run, [`docs/performance/ats-phase4-jd-handoff.md`](docs/performance/ats-phase4-jd-handoff.md) for the Phase 4 handoff measurement, and [`docs/performance/ats-phase4-live-quality.md`](docs/performance/ats-phase4-live-quality.md) for the three-JD live quality audit. A controlled discovery-to-merge A/B with fixed Web candidates and bounded live ATS calls uses `python scripts/benchmark_ats_e2e.py --web-candidates <json> --profile <json> --output <json>`; it makes public ATS requests, requires explicit local inputs, and enforces production hard caps. See [`docs/performance/ats-phase3-controlled-e2e.md`](docs/performance/ats-phase3-controlled-e2e.md) for the result and limitations. The three-provider JD review uses `python scripts/benchmark_ats_quality.py collect ...` to create an uncommitted local sample and then `audit` to emit a count-only gate report; see [`docs/performance/ats-phase5-multiprovider-quality.md`](docs/performance/ats-phase5-multiprovider-quality.md) for this small-sample result and its limits. HTTP compression can be checked with the same-result interleaved A/B in `python scripts/benchmark_ats_compression.py --output <json> --pairs 3`; the bounded three-provider run cut median wire bytes by 79.31% with identical content fingerprints, job counts, and request counts. See [`docs/performance/ats-http-compression-ab.md`](docs/performance/ats-http-compression-ab.md).

ATS Phase 2 provides an optional production enhancement pipeline and remains off by default through `ats_enabled: false`. Official Ashby/Greenhouse/Lever URLs found by Web Search can be added to the local registry with `python scripts/ats_pipeline.py discover`. Greenhouse discovery also recognizes public pages on `job-boards.eu.greenhouse.io`, while the public API still uses the official `boards-api.greenhouse.io` endpoint. Once enabled, `sync --profile <cv-profile.json>` syncs due boards, while `run --profile ...` combines discovery and sync. The pipeline uses public GET only, requests gzip by default with independent compressed and decompressed size limits, and performs deterministic title/location/seniority prefiltering. A standalone `AI` product or team suffix is not a valid role match; explicit phrases such as `AI evaluation`, `AI systems`, and `agent systems` remain AI-role signals. Phase 4 cleans ATS-provided JD text, caps it at 50,000 characters, and hands it to the ranking worker through the same `merge_jobs.py` local run snapshot. Tasks with text skip page fetching; tasks without it use the existing fallback ladder. Web and ATS still share the canonical job table and analysis cache, and the table stores only the JD hash. Once Phase B initializes `data/source_registry.json`, ATS control writes go only to the generic registry and legacy `data/ats_companies.json` stays read-only; the old file remains the fallback when no generic registry exists. `data/ats_sync_state.json` continues to hold low-sensitivity sync summaries. ATS budgets are independent from Web Search; boards may run concurrently, while one Lever board paginates sequentially. If a Greenhouse `content=true` response exceeds 25 MB, one content-free listing retry may run within the same global request budget and records `content_fallback`. The PII-safe public regression remains `python scripts/benchmark_ats.py --output <json> --page-size 50 --max-pages 10` and stores no job descriptions, titles, or URLs. See [`docs/ats-provider-phase1.md`](docs/ats-provider-phase1.md).

## 🔧 Dependencies

- Python 3.10+
- Required: `pdfplumber` `python-docx` `requests`
- Optional: `playwright` (headless fallback; reuses an installed Chromium-based browser, no `playwright install` needed)
- Optional remote browser: `kernel`, `keyring`

```bash
pip install pdfplumber python-docx requests
pip install playwright   # optional
pip install "kernel>=0.94,<1" keyring  # optional remote browser
```

## 📄 License

[MIT](LICENSE)

---

*Built with [Claude Code](https://claude.com/claude-code).*
