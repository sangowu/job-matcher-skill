# ATS deferred job descriptions

Date: 2026-09-27. A live A/B of reading a Greenhouse listing without its job
descriptions and fetching a description per posting the round keeps, against
reading the listing with every description inline.

## Why

A board is fetched whole and filtered locally, which is what makes its result
reproducible. The descriptions come with it, so every description on the board
is paid for and almost none of it is used. Measured over the round of
2026-09-26: 69 boards, 12,561 postings, 18.1 MB, and 54 candidates committed.
The 12,507 descriptions that were downloaded and discarded are 99.6% of them.

Greenhouse is the only provider this applies to: `content=true` is a flag, and
its listing without the flag still carries the title, location, id and URL that
the prefilter and the identity key need. Ashby, Lever and `amazon.jobs` embed
the description in the only listing they serve.

## Result

Eight Irish Greenhouse boards from the shipped catalog, both arms in the same
process against the production parser, same profile, same market scope, same
request budget and concurrency.

| Metric | Inline | Deferred | Change |
|---|---:|---:|---:|
| Boards | 8 | 8 | 0 |
| Postings received | 1,773 | 1,773 | 0 |
| Postings past the prefilter | 6 | 6 | 0 |
| Candidates emitted | 6 | 6 | 0 |
| Candidates carrying a description | 6 | 6 | 0 |
| Listing requests | 8 | 8 | 0 |
| Description requests | 0 | 6 | +6 |
| Wire bytes | 2,728,022 | 149,965 | **-94.5%** |
| Wall clock | 2,034 ms | 1,277 ms | -37.2% |

The candidate sets are identical by identity key, and every candidate's
description is the same length in both arms.

One board measured on its own: GitLab's listing is 364,941 bytes with the
descriptions and 11,330 without, and one description is about 5.5 KB.

## Bounds

Requests go up, bytes go down. The second pass costs one request per kept
posting, so it is bounded by `top_n + precise_buffer` (20 shipped) per sync and
not by how many jobs the boards list. Both passes share one `RequestBudget`, so
the second cannot outspend `ats_requests_per_round`; an exhausted budget stops
the pass rather than failing the boards, and the remaining postings are counted
as `jd_fetch_skipped`.

A description that cannot be fetched leaves its candidate in place with an
empty `jd_text`, counted as `jd_fetch_failed`. The evaluation worker's fallback
ladder reads the page itself in that case -- slower, but the posting had
already been selected over others, so dropping it costs more than the delay.

`ats_defer_jd: false` restores one request per board with the descriptions
inline.

## Evidence boundary

Counts only. No job titles, URLs, descriptions, candidate data or board tokens
are recorded here or in the `ats` metric events; `jd_requests`,
`jd_fetch_failed`, `jd_fetch_skipped` and `content_deferred` are counts and a
flag on the same per-board rows as the listing's own numbers.
