# ATS board catalog

The repository ships Ashby/Greenhouse/Lever adapters that have been benchmarked
across five phases, but until 2026-09-22 the structured discovery channel never
ran in production. This note records why, what changed, and what the change does
not prove.

## Why the channel was dead

Three independent gaps had to line up:

1. `config.json` shipped `ats_enabled: false`.
2. `references/source_seeds.json` declared no `ats_board` source, and
   `data/ats_companies.json` did not exist, so the registry held no board.
3. `_source_record_from_seed()` built its registry record field by field and
   never copied `board_token`. A correctly written `ats_board` seed therefore
   produced a tokenless registry record, and `ats_view_from_registry()` skips
   any source without a token — so the board vanished silently.

Because of (3), fixing (1) and (2) alone would not have helped: seeding a board
was a no-op. `discovery_plan.py` also emitted structured tasks without
`provider` or `board_token`, so even a surviving board produced a task that no
executor could act on.

## What changed

- Seed-to-registry conversion carries `board_token` and `instance`, and
  `merge_seeds()` syncs both, clearing a stale value when a seed drops one.
- Seed validation rejects an `ats_board` without a token, and rejects
  `ats_public_api` for any provider that has no adapter. `ATS_API_PROVIDERS` is
  pinned against `ats_provider.PROVIDERS` by test so the two cannot drift.
- Structured tasks carry `provider`, `board_token` and, for Lever, `instance`.
  A board is fetched by provider identity; `entry_url` is for human review only.
- 18 verified boards were added to the seed catalog, and the repository default
  is now `ats_enabled: true`.

## How the boards were chosen

No token was written from memory. Each candidate was fetched through the
repository's own read-only adapter and kept only if the board answered and its
job locations matched a supported market, with markets derived from those
observed locations rather than assumed from company headquarters.

Of 25 probed tokens, 19 resolved to a reachable board and 17 carried jobs in a
target market. Six never resolved: those companies use an ATS this repository
has no adapter for. Combined with a revalidation of the six existing test
boards, 18 boards were seeded.

Counts are in
[`performance/ats-board-catalog-2026-09-22.json`](performance/ats-board-catalog-2026-09-22.json).

## Rejections worth remembering

- **A board whose location field is not a location.** One large board returned
  386 jobs whose `location.name` values were `Hybrid`, `Distributed` and
  `In-Office` — a work model, not a geography. The list endpoint cannot place
  those jobs in a market at all, so the board was not seeded. This is a property
  of how that employer fills the field, not a parsing defect.
- **A token that resolved to the wrong company.** One guessed token returned a
  real board belonging to a different employer, detectable only because every
  job sat in an unrelated country. Probing before seeding is what caught it.
- **An empty board.** One board that returned 26 jobs on 2026-08-25 returned
  zero on 2026-09-22 while still answering normally. Reachability is not
  coverage, and TTL revalidation exists for exactly this drift.

## What this does not prove

- **No market recall is claimed.** The catalog is a point-in-time observation of
  which boards answered and where their jobs sat. A later run may see different
  vacancies.
- **China has no public ATS coverage here.** These three providers are not in
  common use in that market, so `cn` continues to rely on Web Search and the
  browser route. One global board with Shenzhen jobs was deliberately left out
  rather than widen the China source-access policy for a single employer.
- **Curation does not scale by hand.** 18 boards is a cold start, not a
  strategy. The catalog grows through `board_harvest.py` instead (see below).

## How the catalog grows

The token for any Ashby/Greenhouse/Lever job already appears in the job URL that
Web Search and browser discovery return. `board_harvest.py` recovers it, so one
observed job teaches the system an entire company: the next round fetches that
employer's whole board in a single request.

- `extract_board()` in `_jobutil.py` reads the board slug from a job URL. It
  uses its own patterns rather than extending `_PLATFORM_PATTERNS`, because the
  identity path reads `group(1)` as the job id and adding a capture group there
  would silently shift every strong identity.
- A recovered board is probed once through the same read-only adapter before it
  is trusted. Its markets come from the locations observed in that probe, never
  from the URL or an assumption about the company.
- A board that does not answer, returns nothing, or has no jobs in a supported
  market is counted and dropped, not registered.
- Probing is bounded per run (`--limit`, default 5); the rest wait for the next
  round. Boards already in the seed catalog or the registry are never reprobed.
- Only provider, token, markets and low-cardinality counts are written. The
  candidate's URL is read and discarded; no URL, job title, JD, or CV text
  reaches the registry.
- The probe is the verification, so a successful one marks the board `verified`
  and may enable it. `--no-enable` stops at `verified` when a human should
  approve the last step.

## Expanding the catalog (2026-09-23)

The 18-board catalog came from a single market's worth of hand-curated
guesses, and the 2026-09-23 live run showed what that costs: 20 jobs reached
the report and exactly one of them scored an `apply`. The structured channel
was the only one producing jobs, so the pool it draws from was the ceiling on
the whole run.

155 candidate tokens were probed by the same rule as before — no token is
trusted because it looks right, each is fetched through this repository's own
read-only adapter, and markets come from the locations the board's own jobs
report rather than from where the company is headquartered. The 18 existing
boards were revalidated first, which is also what proves the probe works:
all 18 answered and all 18 still carried target-market jobs.

Of the 155 probes, 40 resolved to a reachable board and 30 were kept:

| Outcome | Count |
|---|---|
| Kept | 30 |
| No jobs in a target market | 6 |
| Board answered with zero jobs | 3 |
| Single placeholder posting | 1 |
| No board at that token (HTTP 404) | 115 |

The 115 misses are the expected cost of the method: a guessed token either
names a real public board or it does not, and one request settles it.

The catalog went from 18 boards to 48 — Greenhouse 14→30, Ashby 4→17, and
**Lever 0→1**, which matters out of proportion to its size: the Lever adapter
has shipped and been benchmarked since phase 1 but had never had a single board
in the catalog to fetch. Ireland, the only market rolled out, went from 11
boards to 22.

Rejections worth naming, because they are gaps rather than verdicts:

- `trivago` posts every job in Düsseldorf, and `references/markets.json` lists
  Munich, Hamburg, Cologne and Frankfurt but no Düsseldorf. The board was
  rejected by a missing alias, not by an absence of hiring.
- `form3` reports locations as `100% Remote (UK)`. The normalizer refuses to
  guess from a work-model string, which is the right default and still loses a
  board that is plainly in a target market.

### What it actually bought

One real sync round per catalog, against the stored CV profile, temp registry
and temp metrics:

| | Boards | Jobs seen | Candidates | Requests | Time |
|---|---|---|---|---|---|
| Before | 18 | 2,649 | 7 | 18 | 3.1s |
| After | 48 | 6,534 | 12 | 48 | 8.1s |

2.5x the jobs and 48 of 48 boards answering, but only five more candidates.
The deterministic prefilter is narrow for a profile naming four LLM/AI roles,
so most of the added volume is filtered out rather than carried forward. The
pool was a real ceiling and it has been raised; it was not the only one.

## What the expansion does not prove

Markets are observed, not declared: a board's claim comes from the jobs it
happened to be showing at probe time, and it keeps that claim until the TTL
brings it back for re-verification. One Lever board exercises the adapter but
says nothing about Lever pagination under load. The probes ran outside the
round request budget and recorded no metrics, so they establish reachability
and nothing about how a round behaves.

Counts are in
[`performance/ats-board-catalog-expansion-2026-09-23.json`](performance/ats-board-catalog-expansion-2026-09-23.json).

### Why the round cap moved

One request fetches one board, measured rather than assumed — a 694-job
Greenhouse board and a 16-job Lever board each cost exactly one request at
`page_size=50, max_pages=10`. 48 boards therefore sit well inside
`ats_requests_per_round: 100`, and the binding limit was `ats_boards_per_round`,
which capped at 30 while the largest market now seeds 44. It moved to 60. Left
at 30 the tail of that market would simply rotate to the next round — visible
in `boards_skipped_by_cap` rather than silent, but still a market that takes
two rounds to sweep.

## Getting the growth out of one machine

`data/source_registry.json` sits under `.gitignore` — the whole `data/`
directory is excluded because it also holds CV text, the job table and
generated reports. Harvested boards therefore live on one machine: reinstalling
the skill resets them, and nobody else benefits. A board token is public
information with no PII in it; it was simply caught by that blanket rule.

`seed_promotion.py` moves the durable part into version control. It promotes a
source only when it is agent-origin, `verified`, still inside its TTL, and its
`entry_url` can be rebuilt deterministically from the provider identity. The
registry stores no URL by design, so a source whose URL cannot be rebuilt — any
non-ATS agent source — is counted and left alone.

Promotion transfers ownership. `merge_seeds()` refuses a seed that collides with
an `agent` source, so the local record is re-origined to `seed` in the same
operation; without that step the next registry merge would fail outright. A
regression pins both halves.
