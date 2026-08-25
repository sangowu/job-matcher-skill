# job-matcher

**English** | [中文](README.md)

Release documentation: [Changelog](CHANGELOG.md) · [v2.3.0 release notes](docs/releases/v2.3.0.md) · [v2.2.0 release notes](docs/releases/v2.2.0.md) · [v2.1.0 release notes](docs/releases/v2.1.0.md) · [v2.0.0 release notes](docs/releases/v2.0.0.md)

> An **agent skill (for Claude Code & Codex)**: give it your **CV + job intent**, and it extracts your CV fields, retrieves matching jobs via **live web search**, and generates an **interactive HTML report**.

A lightweight take on [JobRadar](https://github.com/sangowu/JobRadar) — agent-native by default (web search + subagents + Python scripts), with an optional BYOK isolated browser, borrowing JobRadar's schema, algorithms and UI style.

---

## ✨ Features

- 📄 **CV parsing**: PDF / DOCX / TXT / MD, or pasted text (no OCR).
- 🧠 **Structured extraction**: target roles, skills, seniority, locations, languages; auto-leveling by *relevant* years.
- 🔎 **Live job retrieval**: adaptive batched web search; role wording follows the CV language while target platforms follow the location (Ireland/UK, continental Europe, Australia/NZ, mainland China, and an inference rule for everywhere else).
- 🎯 **5-dimension scoring**: title / seniority / skills / location / must-have, with a five-tier recommendation (strong apply → skip). Deterministic seniority caps and contract validation catch scoring drift.
- 🗂️ **Incremental cache**: three-layer cache (CV / JD / match score); multi-source same-job aggregation with exact job-id matching on regional platforms; auto re-score when the query changes.
- 🛡️ **Untrusted input isolation**: search results and JD text are treated as data and their embedded instructions ignored; report JSON is escaped and links are restricted to http(s).
- 📊 **Interactive report**: two-column layout (job list 30% + detail 70%) + score badges + dark mode + sort/filter/search + 7/30-day runtime health snapshots + zh/en i18n, a self-contained single-file HTML.
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
   │ [parallel subagents] web search + parse + prefilter → [script] merge_jobs (dedup/cache/eval snapshot)
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
│   └── ats_phase1_boards.json # public-company sample for the ATS baseline
├── scripts/              # deterministic Python scripts
│   ├── extract_cv.py         # parse CV → text + hash
│   ├── validate_profile.py   # validate + seniority→levels mapping
│   ├── analysis_contract.py  # validate JDProfile/MatchScore worker output
│   ├── merge_jobs.py         # single writer: dedup/cache/eval snapshots/conditional commit
│   ├── runtime_metrics.py    # PII-safe JSONL events and health calculations
│   ├── summarize_metrics.py  # 7/30-day Markdown/JSON health report
│   ├── round_timer.py        # full-round timing, compared per orchestration mode
│   ├── subagent_metrics.py   # requested/effective subagent model and effort metrics
│   ├── browser_provider.py   # Kernel/Fake providers and safe settings
│   ├── browser_control.py    # remote visual-browser control CLI
│   ├── browser_setup.py      # one-shot localhost setup page
│   ├── browser_workflow.py   # listing pagination/pause state machine
│   ├── benchmark_pipeline.py # fixed small core/Fake Provider benchmark
│   ├── benchmark_ats.py      # bounded PII-safe public ATS API baseline (not production)
│   ├── cp_hash.py            # stable candidate_profile hash
│   ├── verify_jobs.py        # dead-link / closed-posting detection
│   ├── fetch_rendered.py     # headless render fallback (reuses system browser)
│   ├── render_html.py        # render HTML report
│   └── _jobutil.py           # shared: normalization / dedup keys / URL canonicalization
├── assets/template.html  # static report template (Tailwind + vanilla JS)
└── data/                 # runtime data (.gitignored, contains PII)
```

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
| `max_parallel_subagents` | 3 | per-batch parallelism cap |
| `subagent_profiles` | see config | requested model, reasoning effort, and context isolation per role |
| `max_websearch_calls` | 6 | total web-search call cap |
| `stop_threshold` | 12 | stop once enough net-valid jobs found |
| `consecutive_empty_stop` | 2 | stop after N consecutive empty batches |
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

Runtime state has one canonical table, `data/jobs_table.json`. Each evaluation batch gets a minimal `data/eval_runs/<run_id>.json` snapshot. Workers return results, the orchestrator conditionally commits evaluation-owned fields, and a completed snapshot is released after a PII-free summary is appended to `history.jsonl`. Every merge/update also appends a PII-safe event to `data/metrics.jsonl`.

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

The report covers throughput/cache behavior, evaluation success/rejection/conflict rates, subagent success/valid-item/fallback rates grouped by effective model and effort, browser sessions/handoffs, command and lock-wait p50/p95/p99, and backlog state. Every HTML render automatically embeds static 7/30-day snapshots. Threshold violations produce `degraded`; the CLI's `--fail-on-breach` also exits with code 2. See [the monitoring guide](docs/monitoring.md) for definitions and privacy boundaries.

Full-round wall clock is collected separately, because per-script duration is a rounding error next to the search and evaluation work between calls and cannot answer whether overlapped batching pays off:

```text
python scripts/round_timer.py start          # -> {"round_id": "round-..."}
python scripts/round_timer.py finish --round-id <R> --orchestration overlapped|serial
```

The summary reports p50/p95 per mode plus `overlap_saving_pct`, which stays `n/a` until both modes have samples.

Release regressions use a fixed 15-job cold dataset and 10 Fake sessions: `python scripts/benchmark_pipeline.py --output <json> --baseline docs/performance/v2.2.0-small-baseline.json`. The artifact contains raw iterations, p50/p95, absolute and relative changes, with no real web search or cloud-provider calls.

ATS Phase 1 is a development benchmark only and is not part of the normal job-search pipeline: `python scripts/benchmark_ats.py --output <json> --page-size 50 --max-pages 10`. It calls only the official public GET endpoints listed in the sample configuration and does not persist job descriptions, titles, or URLs. See [`docs/ats-provider-phase1.md`](docs/ats-provider-phase1.md) for the design and production-entry gates.

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
