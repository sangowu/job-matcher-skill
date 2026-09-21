# Multi-region Phase D2: bounded public-source live smoke

Phase D2 adds an explicit diagnostic harness for a small four-market source
sample. It does not join the production discovery path, run in default CI,
enable a source, or claim market recall.

## Fixed plan and safety limits

`references/multi_region_smoke_plan.json` fixes one source slot and one
role/location pair for each of `ie`, `uk`, `cn`, and `de`. The harness resolves
the source ID through the validated Phase B seed catalog; result artifacts use
only opaque source slots and source types.

For a public source whose generic entry page does not contain search results,
the plan may provide a same-site absolute `request_path`. Validation rejects a
scheme, host, fragment, non-HTTPS resolution, or cross-site destination. This
keeps the source registry entry generic while allowing the fixed smoke role and
location to exercise a bounded public search page. It does not increase the
request, redirect, response-size, or timeout limits.

The versioned limits are four sources, two requests per source, 512 KiB per
response, an eight-second timeout, and two same-site HTTPS redirects. Literal
IP addresses, local hosts, credential-bearing URLs, cross-site redirects, and
non-HTML responses fail closed. A matching listing may open at most one detail
page. Login or human-verification signals stop that source immediately.

China's initial local sources prohibit direct automation. The 2026-09-18 China
slot was therefore `policy_skip`. On 2026-09-19 it was replaced with the
separately verified public Nankai University campus-job listing. The BOSS,
Liepin, and Lagou restrictions remain unchanged; the harness does not bypass
login, CAPTCHA, or source access controls.

The public China listing filters `AI` and Shanghai in its URL. Its job title
and city are separate elements of each list item, so the parser associates
them only within the same card. Short ASCII role terms use token boundaries;
`AI` in a hostname or in another word cannot create a role hit. A visible
application email on the detail page counts as an application route. These
rules have offline regression tests and do not change production discovery.

Run the smoke only by explicit command:

```bash
python scripts/multi_region_smoke.py --live \
  --output docs/performance/multi-region-live-smoke-20260918.json
```

Without `--live`, the command exits before network access. Unit tests inject a
Fake transport and never use the network.

## Count-only evidence contract

The artifact records source status, failure category, request/byte/duration
counts, candidate funnel counts, links checked, liveness, JD coverage, and
visible application-route counts. It does not store company names, titles,
URLs, queries, page bodies, or JD text. External network, HTTP, size, and access
failures are source/environment evidence; they are not test failures or code
regressions.

## 2026-09-18 bounded run

The checked-in count-only artifact used plan SHA-256
`4b6126ada28f00d0cf384b8007254389875b2b2eb0856e1218ae6c1a5d2be4ac`.

| Market | Result | Requests | Raw job-like links | Fixed role/location matches | Detail links checked |
| --- | --- | ---: | ---: | ---: | ---: |
| Ireland | observed entry page | 1 | 64 | 0 | 0 |
| UK | observed entry page | 1 | 100 | 0 | 0 |
| China | skipped by source policy | 0 | 0 | 0 | 0 |
| Germany | external network failure | 1 | 0 | 0 | 0 |

No detail page was selected, so link validity and JD coverage are unknown, not
zero. Ireland and UK prove only that the selected entry pages were readable in
this run and contained job-like links; the fixed AI-role/location pair was not
observed in the returned entry HTML. Germany and China remain inconclusive.
This sample cannot estimate coverage or recall and does not justify changing
any default.

## 2026-09-18 targeted-listing diagnostic

After the entry-only run, the bounded plan was tightened for the UK and Germany
to use same-site public search paths. This was a diagnostic rerun on the same
UTC date, not another shadow observation and not evidence of repeat-day
stability.

| Market | Evidence conclusion | Raw job-like links | Fixed matches | Detail links checked | Live/JD |
| --- | --- | ---: | ---: | ---: | ---: |
| Ireland | preliminary | 64 | 0 | 0 | unknown |
| UK | sufficient for the live-smoke gate | 80 | 25 | 1 | 1 / 1 |
| China | inconclusive policy skip | 0 | 0 | 0 | unknown |
| Germany | sufficient for the live-smoke gate | 27 | 2 | 1 | 1 / 1 |

The UK and Germany checks each followed exactly one matching detail link,
observed an available JD and application route, and stayed inside the existing
two-request, 512 KiB-per-response, eight-second, same-site limits. Ireland
still has no fixed-role match on the selected public-sector entry page. China
remains a zero-request policy skip. No market was enabled and no formal report
or ranking was changed.

For the later Phase E gate, an observed market is `sufficient` only when the
bounded run checks at least one matching detail link, verifies every checked
link alive, and observes at least one available JD. Entry-only observations
remain `preliminary`; all other outcomes remain `inconclusive`.

## Rollback and next gate

Rollback is additive: remove the plan, harness, tests, and count-only artifact.
The production flag remains `multi_region_enabled=false`, and the legacy
single-region/Web Search flow is unchanged.

Phase E remains separate: shadow/opt-in runs, repeated quality and latency
measurement, real Top-N incremental contribution, and an explicit decision on
whether any source should be enabled by default.

## 2026-09-19 China public-source diagnostic

The new count-only artifact is
`docs/performance/multi-region-live-smoke-20260919.json`. In the bounded
four-market run, the China slot made two public GET requests: one Shanghai AI
listing page and one matching detail page. It observed 26 job-like links, one
fixed role-and-location match, one live detail, one available JD, and one
visible application route. The China result is `sufficient` for this narrow
live-smoke gate, not a claim about market recall, job fit, or repeat-day
stability. The selected source is campus-focused, and the fixed role may leave
the listing; other China channels still need separate evaluation. The run
left every market in `off` mode and did not modify the canonical job table or
formal report.
