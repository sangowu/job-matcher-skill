# Multi-region Phase D1: offline report evidence

Phase D1 makes the Phase C provenance and coverage state visible in the local
single-file report. It is an offline presentation and regression slice: it does
not run a source, perform a live smoke test, claim market recall, change ranking,
or enable multi-region discovery by default.

## Additive run metadata

Legacy `run_meta.json` remains valid with only
`profile_summary/new_count/cached_count/lang`. A multi-region run may add:

```json
{
  "report_language": "zh-Hans",
  "target_markets": ["de", "cn"],
  "search_languages": ["de", "en", "zh-Hans"],
  "run_time": "2026-09-18T10:00:00Z",
  "route_summaries": []
}
```

`route_summaries` is the count-only output from `candidate_handoff.py`.
`render_html.py` aggregates it per target market. A caller may instead provide
`market_coverage[]` directly with `status`, `sources_planned`,
`sources_succeeded`, `sources_failed`, `sources_skipped`, and
`candidates_incremental`.

Coverage status is one of `executed`, `partial`, `failed`, `skipped`,
`not_collected`, or `unknown`. Failed/skipped candidate contribution is shown as
unknown, never as zero. An executed source set with no observed candidates is
labelled a preliminary run result, not a claim that the market has no jobs.

## Job evidence and filters

The renderer derives market, source-type, route, and verification facets from
the canonical job plus `raw_sources[]`. The report provides filters for market,
source type, and verification state. Each detail view shows every provenance
observation's source, source type, discovery route, normalized location, search
language, and link status.

One canonical job still produces one card. Two or more observations add a
multi-source badge; they do not duplicate the card. Original title, company,
salary, and location strings remain unchanged so a Chinese or German source is
not silently translated into an identity or display field.

All values remain embedded through the existing script-safe JSON encoder and
escaped before HTML insertion. Only HTTP(S) application/source URLs remain
clickable.

## Evidence boundary and rollback

The four-market fixtures and report tests prove deterministic rendering,
filterable facets, multilingual preservation, empty-state distinctions, and
injection protection. They do not establish live availability, link-validity
rates, JD completeness, market recall, or actual Top-N contribution.

Rollback is additive: omit the new metadata or keep
`multi_region_enabled=false`. Legacy jobs lacking Phase C provenance display
unknown market/source/verification values and remain renderable.

The next separate slice is Phase D2: bounded, explicitly invoked public-source
live smoke plus per-market evidence boundaries. Phase E shadow/opt-in rollout
and production-default enablement remain later work.
