# job-matcher

**English** | [中文](README.md)

Release documentation: [Changelog](CHANGELOG.md) · [v2.1.0 release notes](docs/releases/v2.1.0.md) · [v2.0.0 release notes](docs/releases/v2.0.0.md)

> An **agent skill (for Claude Code & Codex)**: give it your **CV + job intent**, and it extracts your CV fields, retrieves matching jobs via **live web search**, and generates an **interactive HTML report**.

A lightweight take on [JobRadar](https://github.com/sangowu/JobRadar) — pure agent-native capabilities (web search + subagents + Python scripts), **zero external services**, borrowing JobRadar's schema, algorithms and UI style.

---

## ✨ Features

- 📄 **CV parsing**: PDF / DOCX / TXT / MD, or pasted text (no OCR).
- 🧠 **Structured extraction**: target roles, skills, seniority, locations, languages; auto-leveling by *relevant* years.
- 🔎 **Live job retrieval**: adaptive batched web search; market switches by CV language.
- 🎯 **5-dimension scoring**: title / seniority / skills / location / must-have, with a five-tier recommendation (strong apply → skip).
- 🗂️ **Incremental cache**: three-layer cache (CV / JD / match score); multi-source same-job aggregation; auto re-score when the query changes.
- 📊 **Interactive report**: two-column layout (job list 30% + detail 70%) + score badges + dark mode + sort/filter/search + 7/30-day runtime health snapshots + zh/en i18n, a self-contained single-file HTML.

## 🏗️ Architecture

- **Main agent = orchestrator**: runs scripts, fuses the query, asks the user, spawns subagents.
- **Subagents do the heavy-context work** (CV extraction / search / scoring): raw text stays inside subagents; the main context only carries "paths + small JSON".
- **Python scripts do the deterministic work**: parse, validate, dedup/aggregate/cache, verify, render.
- **Parallel compute, serialized commits**: search and evaluation workers may overlap, while `jobs_table.json` has one guarded write path with evaluation snapshots, a cross-process lock, and atomic replacement.

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

**Fallback ladder** (shared by liveness check & JD fetch): `WebFetch → requests static fetch → playwright headless (reuses system default browser, no extra download) → mark "unverified" without blocking`.

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
│   └── search_playbook.md    # fan-out / per-market / adaptive batching
├── scripts/              # deterministic Python scripts
│   ├── extract_cv.py         # parse CV → text + hash
│   ├── validate_profile.py   # validate + seniority→levels mapping
│   ├── analysis_contract.py  # validate JDProfile/MatchScore worker output
│   ├── merge_jobs.py         # single writer: dedup/cache/eval snapshots/conditional commit
│   ├── runtime_metrics.py    # PII-safe JSONL events and health calculations
│   ├── summarize_metrics.py  # 7/30-day Markdown/JSON health report
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
| `max_websearch_calls` | 6 | total web-search call cap |
| `stop_threshold` | 12 | stop once enough net-valid jobs found |
| `jd_ttl_days` | 30 | JD cache validity |
| `seniority_mode` | balanced | strict / balanced / stretch |
| `enable_headless_fallback` | true | headless fallback switch |
| `headless_budget` | 3 | headless calls per run |
| `table_lock_timeout_seconds` | 10 | maximum wait for the canonical-table write lock |
| `stale_lock_seconds` | 120 | age at which an abandoned lock may be reclaimed |
| `monitoring_default_window_days` | 7 | default health-report window |
| `monitoring_thresholds` | see config | conflict, rejection, success, lock-wait, and backlog limits |

Runtime state has one canonical table, `data/jobs_table.json`. Each evaluation batch gets a minimal `data/eval_runs/<run_id>.json` snapshot. Workers return results, the orchestrator conditionally commits evaluation-owned fields, and a completed snapshot is released after a PII-free summary is appended to `history.jsonl`. Every merge/update also appends a PII-safe event to `data/metrics.jsonl`.

## 📈 Runtime monitoring

```text
python scripts/summarize_metrics.py --days 7 --format markdown
python scripts/summarize_metrics.py --days 30 --format json
python scripts/summarize_metrics.py --fail-on-breach
```

The report covers throughput/cache behavior, evaluation success/rejection/conflict rates, command and lock-wait p50/p95/p99, plus active runs, pending tasks, and oldest backlog age. Every HTML render automatically embeds static 7/30-day snapshots behind the header status control. Threshold violations produce `degraded`; the CLI's `--fail-on-breach` also exits with code 2. See [the monitoring guide](docs/monitoring.md) for definitions and privacy boundaries.

## 🔧 Dependencies

- Python 3.10+
- Required: `pdfplumber` `python-docx` `requests`
- Optional: `playwright` (headless fallback; reuses an installed Chromium-based browser, no `playwright install` needed)

```bash
pip install pdfplumber python-docx requests
pip install playwright   # optional
```

## 📄 License

[MIT](LICENSE)

---

*Built with [Claude Code](https://claude.com/claude-code).*
