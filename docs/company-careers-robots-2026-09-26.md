# What the ten company-careers hosts actually permit

Measured 2026-09-26 with `scripts/check_robots.py`. One request per host for
`robots.txt`, one for the entry URL, five seconds apart, with a user agent that
names this client. Every `robots.txt` was read with RFC 9309 longest-match
precedence.

Before this, ten seeds carried `verified: true`,
`verification_method: official_global_job_search` and
`automation_allowed: true`, and only `amazon.jobs` had ever been looked at. The
catalog was asserting a verification nobody had performed.

## Result

| source | robots.txt | entry URL | entry | with a query | paginated |
|---|---|---|---|---|---|
| amazon-careers | 200 | 200 | allowed | allowed | allowed |
| microsoft-careers | 200 | 200 | allowed | allowed | allowed |
| apple-jobs | **404** | 200 | allowed | allowed | allowed |
| sap-careers | 200 | 200 | allowed | allowed | allowed |
| siemens-jobs | 200 | 200 | allowed | allowed | allowed |
| ibm-careers | 200 | 200 | allowed | allowed | allowed |
| google-careers | 200 | 200 | allowed | allowed | **forbidden** |
| accenture-careers | 200 | 200 | allowed | **forbidden** | **forbidden** |
| oracle-careers | 200 | 200 | allowed | allowed | allowed |
| bosch-careers | 200 | 200 | allowed | allowed | allowed |

"with a query" is `?q=ai+engineer`; "paginated" adds `&page=2`. Checking only
the entry URL would have called all ten usable.

## The two that are not what the catalog said

**accenture-careers** — `Disallow: */careers/jobsearch?`. The landing page is
allowed and every search is not, and a search is the only use this source has.
`automation_allowed` is now `false` and `public_read_only_page` is gone;
`web_search` and `manual_browser` stay, because a person opening the page in
their own browser is not what robots.txt addresses.

**google-careers** — four rules forbid `?page=` on the jobs results path.
Reading the first page is allowed; paging is not, and the browser workflow pages
up to `browser_max_pages`. Recorded as `robots_disallow_result_pagination` on
the seed, which `discovery_plan` copies onto every browser task built from it.

## Two readings that would have been wrong

**Microsoft.** `apply.careers.microsoft.com/robots.txt` is

```
User-agent: *
Disallow: /
Allow: /careers
```

`urllib.robotparser` returns the first matching rule in file order and reads
this as "everything is forbidden". RFC 9309 §2.2.2 says the longest matching
path wins, so `/careers` is allowed — which is plainly what the operator meant
by writing the Allow line. This is why `check_robots.py` exists instead of a
call to the standard library, and a test pins the disagreement.

**Google and Yandex.** `google.com/robots.txt` forbids Yandex the whole
`/about/careers/applications/jobs/results` path, in a group of its own. Reading
another agent's group as ours would have disabled a source nobody closed to us.

**Apple** publishes no `robots.txt` at all (404). RFC 9309 reads an absent file
as no restrictions published. That is not permission granted by silence, and it
is not a refusal either; it is simply nothing to obey.

## What is still unverified

These checks cover `robots.txt` and that the entry URL answers. They do not
cover each operator's written terms of service, which are prose and are not
machine-readable. Two sources in the catalog carry
`verification_method: operator_terms_reviewed` because their terms were read by
hand; these ten do not, and nothing here claims they were.
