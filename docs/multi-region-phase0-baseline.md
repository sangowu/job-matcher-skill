# Multi-region Phase 0 contract and baseline

Recorded on 2026-09-17 before the Phase A implementation. This document is a
compatibility baseline, not evidence of live-market coverage.

## Locked identifiers and budgets

- Market IDs: `ie`, `uk`, `cn`, `de`.
- Internal language codes: `en`, `de`, `zh-Hans`; boundary values `zh` and
  `zh-CN` normalize to `zh-Hans`.
- One search run keeps the existing hard limit of six Web Search calls.
- Deterministic regional sources use a separate planning boundary. Phase C now
  provides an explicit handoff boundary; enabling it in the default workflow
  and tuning execution budgets remain later rollout work.
- Every requested market must receive one generic query slot before a market
  receives another slot. If the Web budget cannot cover every market, planning
  returns `needs_user_input=true` instead of silently dropping a market.

## Frozen discovery contracts

The future `CandidateEnvelope` requires `title`, `company`, `url`, `source_id`,
`source_type`, `discovery_route`, `search_language`, `observed_at`,
`identity_keys`, `link_verification_status`, and `location_normalized`. Optional
display fields remain `location`, `snippet`, `date_posted`, and `salary`.
Discovery workers must not attach `jd_text`, scores, CV data, or personal data.

`raw_sources[]` remains the provenance source of truth after merge. Each future
provenance entry will preserve `source_id`, `source_type`, `discovery_route`,
`search_language`, `observed_at`, and link-verification status. Market ID is
metadata, never a job identity key.

`EvalHandoff` remains run-scoped and contains only `record_id`, `dedup_key`,
`base_record_version`, `jd_input_hash`, body source, truncation state, and at
most 50,000 characters of transient `jd_text`. Existing completion/conflict
cleanup and hash-based invalidation remain authoritative.

The source registry state machine was frozen in Phase 0 as
`candidate -> verified -> unavailable`, with successful reverification allowed
from `unavailable`; `disabled` is only a local/user decision. Phase B now
implements that contract without connecting it to source execution; see
[`source-registry-phase-b.md`](source-registry-phase-b.md).

## Compatibility and rollback

`multi_region_enabled` defaults to `false`. `scripts/market_plan.py` is an
explicit, read-only planning boundary. It does
not run automatically, call a source, or write `jobs_table.json`. Therefore the
existing single-region Web Search flow, ATS registry, cache keys, merge/update
contracts, scoring, and report rendering remain unchanged.

The regional-source execution pipeline remains opt-in after Phase C. Rollback
is to keep `multi_region_enabled=false` and stop invoking the planner/handoff;
this neither deletes registry state nor changes the readability of an existing
jobs table.

## Pre-implementation verification baseline

- Git: `main...origin/main`; only
  `docs/multi-region-implementation-todo.md` was untracked.
- Ruff: passed.
- Pytest: 175 passed, 1 failed. The failure was the existing
  `test_legacy_table_is_migrated_in_place`, whose fixed `last_seen=2026-08-01`
  crossed the 30-day archive TTL on 2026-09-17. The production archive logic
  was correct; the test clock was not fixed. Phase A stabilizes only that test's
  clock and does not change merge/archive behavior.
- Existing candidate input: Web/ATS candidate arrays enter one
  `merge_jobs.py merge` writer. Existing ATS registry remains
  `data/ats_companies.json`; Phase A does not migrate it.
- Existing output: one canonical jobs table, run-scoped evaluation snapshots,
  cached JD analysis/match scores, and one final HTML render.

## Offline fixture and evidence boundary

`tests/fixtures/multi_region/candidates.json` contains ten synthetic candidate
observations per market. It includes four discovery routes, cross-route strong
identity duplicates, same-title distinct identities, closed links, missing
locations, remote/hybrid work, and local-language plus English records for
China and Germany. `ground_truth.json` fixes eight unique strong identities per
market and the expected duplicate groups.

The fixture tests routing and future merge inputs only. It contains no real CV,
personal data, full JD, or live source evidence and cannot support a claim about
market-level precision, recall, availability, or source quality.
