# Local-browser Phase 4: bounded runtime smoke

Observed in Codex desktop on Windows. The authorized Chrome fallback was
verified at `2026-09-21T10:41:40Z`; BrowserOS Neo MCP was then verified at
`2026-09-21T10:46:56Z` after its tool surface became available. A real
job-board login handoff and post-login search were verified at
`2026-09-21T11:03:32Z`. One live candidate extraction, detail liveness check,
CandidateEnvelope validation, and isolated merge completed at
`2026-09-21T11:14:07Z`. A bounded three-candidate same-page batch completed at
`2026-09-21T15:34:48Z`.

## Result

The first observation did not expose BrowserOS Neo, so `auto` correctly used
the authorized user-browser fallback. A later observation exposed the Neo MCP
tool surface. The Agent named a Neo session, then completed a bounded read-only
smoke in one dedicated tab:

1. Opened a newly owned tab on a public BrowserOS page.
2. Read non-empty page content.
3. Navigated the same tab to another public BrowserOS page.
4. Read non-empty content and observed the expected changed page title.
5. Closed the dedicated tab in a `finally` cleanup path.

The capability attestation `{tabs,navigate,read}` produced
`browseros_neo=ready`. With the authorized user browser and model search also
available, default `auto` selected only `browser` with
`browser_provider=browseros_neo`, `status=ready`, and no issues. It did not run
the lower-priority browser or model routes concurrently.

The next route, an authorized Chrome browser connection, passed a bounded
read-only smoke:

1. Created a newly named Agent tab without listing or claiming existing tabs.
2. Opened a public BrowserOS page.
3. Navigated the same dedicated tab to another public BrowserOS page.
4. Read the visible accessibility content and observed the changed page title.
5. Closed the dedicated tab.

That earlier fallback observation remains useful: its capability attestation
produced `user_browser=ready`, and default `auto` selected only that browser
route while Neo was unavailable. It did not start model search concurrently.

Count-only evidence is stored separately for
[`BrowserOS Neo`](performance/local-browser-phase4-neo-smoke-2026-09-21.json)
and the earlier
[`authorized user-browser fallback`](performance/local-browser-phase4-user-browser-smoke-2026-09-21.json).

## Authenticated job-board handoff

A dedicated Neo tab opened LinkedIn Jobs and detected that sign-in was
required. The site was paused without challenge bypass or fallback to another
browser. The loopback panel received
`needs_user_action/browseros_neo/login_required`, rendered its attention state
and resume control, and later emitted `resume_requested` after the user acted.

The Agent then opened a fresh dedicated tab without enumerating or taking over
existing tabs. The LinkedIn Jobs landing page exposed authenticated navigation
and no login form, guest sign-in control, or checkpoint path. In the same tab,
the Agent navigated to one bounded Ireland software-engineering search,
observed a result container, and read non-empty page content. The verification
tab was closed and the panel status was set to `completed`.

The search page did not itself expose the same authenticated-navigation marker,
so this evidence proves login reuse at the Jobs landing page followed by
same-tab search reachability; it does not claim that every LinkedIn search-page
layout exposes or requires authenticated UI. Count-only evidence is stored in
[`performance/local-browser-phase4-auth-handoff-smoke-2026-09-21.json`](performance/local-browser-phase4-auth-handoff-smoke-2026-09-21.json).

## Candidate extraction and isolated merge

The Agent selected one public LinkedIn result, opened its canonical detail URL,
and restricted the successful read to the page's `main` subtree. The scoped
parser extracted one company, title, Ireland location, remote scope, stable
LinkedIn job identity, and active Apply signal with no closed-posting signal.
It returned only CandidateEnvelope fields and bounded verification booleans.

The candidate passed `candidate_contract.py` with
`discovery_route=browseros_neo`, `source_type=global_job_board`, and an `alive`
link status. `browser_candidate_smoke.py` then ran the real merge writer against
a system temporary store: one candidate validated, one strong-identity record
was created, one analysis task was emitted, and both browser route and source
type provenance were preserved. The temporary store was removed automatically;
the canonical job table was not used.

The first exploratory detail read was not scoped and returned an account-nav
notification count in ephemeral tool output. That attempt is classified as a
privacy regression and is not success evidence. No value from it was written to
the repository or candidate data. The successful retry used subtree-scoped
reading and prompted explicit workflow guidance preventing account navigation,
notification counts, and personalized sidebars from entering candidates or
logs. Count-only evidence is stored in
[`performance/local-browser-phase4-candidate-smoke-2026-09-21.json`](performance/local-browser-phase4-candidate-smoke-2026-09-21.json).

A later bounded batch reused the same scoped parser on one Ireland results page.
It extracted three unique strong-identity candidates, opened three detail pages
in parallel, and returned no account chrome. One detail had an active Apply
signal and two conservatively remained `unknown`; none was promoted to `alive`
without direct evidence. All three passed CandidateEnvelope and temporary merge,
producing three table records and three analysis tasks with 3/3 browser-route
and source-type provenance retention. Count-only evidence is stored in
[`performance/local-browser-phase4-batch-smoke-2026-09-21.json`](performance/local-browser-phase4-batch-smoke-2026-09-21.json).

## Public-page smoke privacy boundary

- Existing tabs enumerated or read: `0`.
- Account/private pages opened: `0`.
- Cookies, browser profiles, account identifiers, history, CV/JD content, or
  session IDs captured: `0`.
- Forms submitted, messages sent, files uploaded, applications submitted, or
  account state changed: `0`.
- CAPTCHA, login, consent, and rate-limit handling were not provoked during the
  public-page smokes.

The authenticated handoff stored no account identifier, URL/query, page text,
job title, company, CV/JD content, Cookie/profile data, tab/session ID, or
credential. It submitted no form, application, message, upload, save, follow,
or account mutation. Only allowlisted panel state and count-only evidence were
persisted.

The candidate smoke persisted public job fields only in transient tool input
and a temporary merge store. Repository evidence contains counts, source class,
route, and pass/fail boundaries but no title, company, job URL/ID, page text, or
account metadata.

## Full-CV production follow-up

On 2026-09-22, a Dublin-first Ireland run closed the earlier production-table
gate. A real local CV passed through three discovery waves, canonical production
merge, two JD evaluations, and a two-job interactive HTML report. Every planned
task reached a terminal state; blocked sites were recorded as explicit failures
or skips rather than false zero-result successes.

The trial also exposed a metrics boundary: direct BrowserOS Neo MCP actions did
not emit the repository's `browser` runtime events, so run completeness and
report health remained `unknown`. That path now exists. Counts, privacy boundaries, and failure
classes are documented in
[`browseros-neo-production-trial-2026-09-22.md`](browseros-neo-production-trial-2026-09-22.md).

## Remaining gate

Together, these runs prove the Neo-first path, authorized user-browser fallback,
real login pause/resume, authenticated-session reuse, bounded search, scoped
candidate extraction, contract validation, isolated merge, and one full
single-market CV production run. Local-browser runtime-metric completeness is no
longer a gate: an Agent-driven browser now reports each action through
`browser_control.py action`, which writes the same allowlisted `browser` event
the remote adapter emits. The runs still do **not** prove additional-page
pagination, broad job-board/layout/language compatibility, ambiguous-consent
recovery, custom-combobox compatibility, CAPTCHA recovery, or rate-limit
pause/resume. A redirect to a public ATS host is no longer a boundary failure:
it is recorded as a board handoff and fetched through the structured channel. A public Skill must continue to derive capability from the current
Agent tool surface and degrade safely rather than treating installation or
documentation as live evidence.
