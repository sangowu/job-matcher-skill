# Multi-region Phase C: dual discovery and canonical merge

Phase C adds an explicit control plane for combining deterministic regional
sources with Agent Web Search. It does not enable multi-region discovery by
default, add report UI, perform live smoke tests, or change scoring.

## Execution boundary

The orchestrator starts the `regional_registry` and `agent_web_search` workers
in the same orchestration step. Workers only return immutable route batches;
they never write `jobs_table.json`, evaluation snapshots, the source registry,
or runtime metrics directly. A failed route still returns a bounded report with
`status=failed`, an approved low-cardinality `failure_kind`, and no candidates.

After both routes have reported `succeeded`, `failed`, or `skipped`, the
orchestrator submits one object to:

```text
python scripts/candidate_handoff.py --cv-hash H --cp-hash H \
  --metrics-run-id R < route_batches.json
```

The input contains one stable `batch_id`, the selected market/source plans, the
two route reports, and optional PII-safe source proposals/events. The handoff
requires both route reports; one route failing does not discard candidates from
the other route.

## Candidate contract

`references/candidate_envelope.schema.json` is the formal discovery schema and
`scripts/candidate_contract.py` is the dependency-free runtime validator.
Candidates use only internal languages `en`, `de`, and `zh-Hans`; reference a
known `source_id`; include UTC `observed_at`; and carry bounded display fields,
strong provider identity keys, normalized location metadata, and link status.
Discovery candidates cannot carry JD text, CV data, scores, `jd_profile`,
`verified`, or `scored_from`.

`raw_sources[]` in the canonical job is the provenance source of truth. Each
observation preserves `source_id`, `source_type`, `discovery_route`,
`search_language`, `observed_at`, link status, normalized location, and its URL.
Top-level `market_ids` is a derived union for filtering only; it never
participates in identity. Provider-owned strong IDs continue to decide whether
translated/cross-route observations are the same job.

Legacy candidate arrays without `discovery_route` remain readable through the
existing merge path. Existing table rows are normalized only when the merge
writer next loads them: missing provenance is filled with `unknown`,
`market_ids=[]`, and `market_status=unknown`. There is no destructive rewrite.

## Commit order and recovery

The handoff commits in this order:

1. validate the full input and all CandidateEnvelopes;
2. invoke the existing `merge_jobs.py merge` single writer with `--batch-id`;
3. commit source proposals/events through the source-registry single writer;
4. mark the count-only handoff manifest complete and emit route metrics.

`merge_jobs.py` stores a bounded applied-batch marker in the canonical table.
Replaying the same ID and canonical candidate hash returns success without
incrementing `seen_count` or creating another evaluation task; reusing an ID
with different candidates is rejected. `data/candidate_runs/<batch_id>.json`
records only hashes, phases, paths/count summaries, and commit results—never
candidate payloads, queries, titles, companies, URLs, CVs, or JDs.

If source-registry commit fails after merge, the manifest remains at
`merge_committed`; retry skips merge and resumes the source commit. If the
process stops after the table commit but before the manifest update, the merge
batch marker makes the retry idempotent. The required ordering is always jobs
first, source state second.

## Metrics and cache behavior

Runtime metrics schema v6 adds one `discovery` event per route batch. Allowed
dimensions are low-cardinality market, source type, route, and language; all
other fields are counts/duration or an approved failure category. Merge events
also report additive provenance migrations and batch replays.

For deterministic discovery attribution, regional-registry identities own the
first contribution within a market; an Agent Web Search identity already seen
by that market's regional route is counted in `duplicate_intersection`, and its
`candidates_incremental` contribution is reduced accordingly. JD and Top-N
contribution fields remain zero until later evaluation/report phases can supply
real evidence; Phase C does not infer those outcomes.

The existing cache keys are unchanged: JD analysis is per canonical job and
match score is per job plus `cv_hash:candidate_profile_hash`. Changing the
target markets or candidate constraints changes the candidate-profile hash and
therefore requests a new score; unchanged constraints reuse old scores. A
candidate already present in an active evaluation snapshot remains
`in_evaluation` and is not dispatched twice.

## Rollback and current limits

Keep `multi_region_enabled=false` or stop invoking `candidate_handoff.py` to
return to the existing single-region Web Search/ATS flow. Phase C files are
additive, legacy candidates remain readable, and old job rows are not deleted.

Phase D1 now consumes this provenance in the offline report; see
[`multi-region-phase-d1.md`](multi-region-phase-d1.md). Still out of scope are
multi-market live smoke, real Top-N contribution/quality evidence,
shadow/opt-in rollout automation, production-default enablement, calibration
evidence, and source-specific expansion beyond the Phase B registry.
