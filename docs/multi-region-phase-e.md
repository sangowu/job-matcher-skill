# Multi-region Phase E: shadow and opt-in rollout gate

Phase E adds a count-only evidence ledger and a deterministic per-market gate.
It does not execute discovery by itself, mutate the canonical jobs table,
change report ranking, or enable any market.

## Read-only shadow comparison

`references/shadow_compare_v2.schema.json` defines new ephemeral inputs accepted
by `scripts/shadow_compare.py` (v1 remains readable for historical runs). It
contains only strong provider identity keys,
match scores, and boolean JD/link evidence for the current formal Top-N and the
two shadow routes. It rejects titles, companies, URLs, queries, CV/hash values,
JD text, board tokens, missing scores, and unmeasured JD flags.

The comparator joins candidates by any shared strong identity key, gives an
existing baseline record precedence, and assigns a new cross-route duplicate
to `regional_registry` before `agent_web_search`. It calculates incremental
unique candidates, route overlap, JD/link counts, and potential Top-N
contribution entirely in memory. Formal baseline entries win equal-score ties,
so a shadow candidate cannot claim contribution merely by tying the current
cutoff.

The v2 input must explicitly set each market's `baseline_status` to `complete`
or `unavailable`. `complete` means the legacy Web Search Top-N was actually
searched, checked and scored for the same CV, role intent, market and observation
window; `unavailable` requires an empty `baseline_top_n`. A completed search may
also return an empty Top-N, but that is a cold-start result, not evidence of
incremental improvement. The comparator derives `baseline_top_n_count` and the
number of incremental candidates with both an available JD and a verified live
link. This is an operator-supplied evidence claim, not independent proof: keep
the underlying CV-specific search/JD/score work locally in ignored `data/` and
audit it before marking a baseline complete. Do not use demo-scored jobs for a
different CV or market.

The output contains no identity keys and is already a valid
`shadow_run_v2.schema.json` object. Pass ephemeral input through stdin when
possible:

```bash
python scripts/shadow_compare.py --record
```

`--record` sends only the count-only summary to the same ledger writer. Without
it, the command performs a dry comparison and prints the safe summary. Neither
mode imports merge, scoring, or rendering code, and neither reads or writes the
canonical job table or report.

## Shadow evidence contract

`references/shadow_run.schema.json` defines one shadow observation. A run must:

- use a stable `shadow-YYYYMMDD-HHMMSS-xxxxxx` run ID and UTC timestamp;
- assert `mode=shadow` and `ranking_unchanged=true`;
- report each market separately;
- include both `regional_registry` and `agent_web_search` route results;
- record only incremental candidates, duplicate intersection, JD handoff and
  coverage counts, live-verification counts, and potential Top-N contribution;
- contain no query, URL, title, company, CV/hash, board token, exception text,
  page content, or JD text.

Record a completed orchestration summary through the single-writer ledger:

```bash
python scripts/shadow_gate.py record \
  --input data/current-shadow-summary.json
```

The ignored runtime ledger is `data/multi_region_shadow_runs.json`. Replaying
the same run ID and counts is idempotent; reusing an ID with different counts
is rejected. The recorder writes only this ledger and never calls merge,
scoring, or report rendering.

## Per-market release gate

A market is eligible for a later default-enablement decision only when all of
these conditions hold:

1. at least three successful shadow runs exist for that market;
2. those runs span at least two distinct UTC dates;
3. every counted successful run passed deterministic acceptance and both
   discovery routes succeeded;
4. the latest supplied live-smoke artifact marks that market's evidence
   `sufficient`.
5. at least three successful v2 runs spanning two dates have a completed,
   non-empty legacy Top-N for the same evaluation scope, and those runs contain
   at least one incremental candidate with both an available JD and a verified
   live link.

Historical v1 runs stay hash-valid and readable, but cannot satisfy condition
5 because their empty baseline arrays have no provenance. Three repeated empty
v2 runs also cannot open the gate. The gate reports both raw successful-run
counts and baseline-complete-run counts; neither alone proves market-wide
precision or recall. Aggregate potential Top-N contribution excludes v1 and
baseline-unavailable runs, so old empty-baseline comparisons do not appear as
measured gains in the refreshed status artifact.

`sufficient` is intentionally stricter than entry-page reachability: the
bounded smoke must have checked at least one matching detail link, verified all
checked links alive, and observed at least one available JD. `preliminary`,
failed, skipped, missing, or otherwise inconclusive evidence blocks default
enablement.

Evaluate the current ledger and live evidence with:

```bash
python scripts/shadow_gate.py status \
  --live-smoke docs/performance/multi-region-live-smoke-20260918.json \
  --output docs/performance/multi-region-phase-e-gate-20260918.json
```

## Rollout configuration and compatibility

`config.json` keeps the existing master flag `multi_region_enabled=false` and
adds a required per-market decision map:

```json
"multi_region_rollout": {
  "ie": "off",
  "uk": "off",
  "cn": "off",
  "de": "off"
}
```

Allowed values are `off`, `shadow`, `opt_in`, and `default`. When the master
flag is false, every effective mode remains `off`, regardless of a requested
per-market mode. A requested `default` mode is invalid unless that market's
gate is eligible, and its effective mode remains `off` when invalid. There is
deliberately no global default-enable switch;
markets must advance independently.

The existing Ireland/general Web Search plan remains authoritative while the
master flag is false. Shadow observations cannot enter the report or alter its
ordering because the ledger accepts only aggregate counts and has no job-table
or renderer write path.

## Current evidence state

The 2026-09-18 gate artifact contains zero recorded shadow runs. The Phase D2
live evidence is preliminary for Ireland and the UK and inconclusive for China
and Germany, so all four markets are currently ineligible. Configuration is
valid because the master flag and every per-market mode remain off.

No synthetic fixture or repeated same-day execution is counted as a successful
real shadow run. At least two actual dates are required before any market can
pass this gate.

## 2026-09-19 shadow observation

One additional real, count-only shadow run was recorded for all four markets
using the previously confirmed generic CV. The fixed public-source diagnostic
made seven read-only requests in total and observed all four source entries;
China made two requests rather than being skipped. Six Agent Web Search slots
were used across the four markets, including Chinese and English for China and
German and English for Germany. Unverified or non-strong-identity results were
excluded from the comparator, not counted as new jobs.

| Market | Successful runs / distinct UTC dates | Incremental candidates in this run | Latest live-smoke evidence |
| --- | ---: | ---: | --- |
| Ireland | 2 / 2 | 0 | inconclusive (entry only) |
| UK | 2 / 2 | 2 | sufficient |
| China | 1 / 1 | 0 | sufficient for one campus-focused source |
| Germany | 1 / 1 | 1 | sufficient |

The current status is saved as
`docs/performance/multi-region-phase-e-gate-20260919.json`; no market is
eligible. The repository's formal job table contains only three demo-scored
records for a different CV/profile, so this run had no comparable formal Top-N
baseline for the selected CV. Its `potential_top_n_contribution` values are
therefore relative to an empty scored baseline and must not be interpreted as
measured improvement over a real report. In particular, China's successful
route execution with zero qualifying strong-identity candidates is not proof
of useful market coverage. The ephemeral comparison input was deleted after
the count-only run was recorded; the canonical job table, report, and rollout
flags were unchanged.

## 2026-09-21 gate hardening

The v2 comparator/ledger contract adds explicit baseline completion and
qualifying-candidate counts. Existing v1 ledger entries remain readable with
their original hashes, but are fail-closed for default enablement. The
2026-09-19 artifact predates this rule; regenerate status before relying on it.
The initial September 21 read-only Web Search sample was not a completed
Top-N baseline. A subsequent isolated six-query observation (2026-09-20 UTC,
2026-09-21 Dublin time) used the locked CV hash `b0b3963dc8d10f0d`, with one
English slot for Ireland and the UK and Chinese/English and German/English
slots for China and Germany. The ignored local audit file
`data/shadow_baseline_20260920.json` retains the exact queries, employer URLs,
JD-derived scoring dimensions and exclusions; it is not copied into the
count-only ledger. Ireland's bounded observation completed with an empty
strong-ID Top-N after the available Dublin AI role proved to require 8+ years;
the UK, China and Germany each had one employer-page-verified, scored Top-N
candidate. The UK lead was carried from the immediately preceding sample and
reverified during this observation. Work authorization remains unknown for the
UK and Germany, and no market-level recall is inferred from six queries.

This baseline audit was dry-run through the v2 comparator with both shadow
routes explicitly skipped. It was **not** recorded as a successful shadow run,
did not change the canonical job table or report, and did not enable any
market. A real dual-route v2 comparison is still required before the new gate
can accrue qualifying runs. The current comparator accepts provider-owned
Workday requisition IDs observed on rendered pages; the short Workday URLs
used by two jobs do not currently yield those keys through URL-only
canonicalization, so future shadow batches must carry the verified provider ID
explicitly or be treated as non-comparable rather than silently matched.
