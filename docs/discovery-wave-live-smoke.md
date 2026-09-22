# Discovery-wave live smoke

On 2026-09-21, a bounded Ireland wave verified the new cross-channel execution
contract against BrowserOS Neo and one live Web Search call.

The generated first wave contained three browser tasks from different source
categories and one localized Web Search task. BrowserOS Neo opened dedicated
tabs and used semantic accessibility controls only. Two sites presented cookie
consent choices, so those tasks entered `needs_user_action` with
`consent_required`; the Agent did not choose on the user's behalf. The third
site completed a keyword-and-location search and returned a valid empty result.

The Web Search task completed once and exposed current direct job pages even
though the successful platform search returned no candidates. This demonstrates
why coverage mode keeps browser-platform discovery and open-web discovery in
the same first wave.

The wave was deliberately not committed and wave 2 was not dispatched. A wave
with paused consent tasks does not yet contain one terminal result per task, so
advancing would violate the handoff contract. The local control-panel state was
set to `needs_user_action/consent_required`; the dedicated tabs remain available
for the user to resolve.

The count-only evidence is in
[`performance/discovery-wave-live-smoke-2026-09-21.json`](performance/discovery-wave-live-smoke-2026-09-21.json).
It stores no CV, query, candidate, URL, title, company, JD, cookie, or account
content. This is a routing and pause-boundary smoke, not a recall measurement or
a completed CV-to-JD production run.

## Completed three-wave run

Later on 2026-09-21, the same bounded Ireland plan was resumed under the new
`necessary_only` classifier. Three unambiguous consent dialogs were handled by
exact semantic labels; no accept-all or category-toggle action was used. One
login-only source and one ambiguous consent control were explicitly skipped,
so neither blocked the other tasks nor became a false success.

All three Web Search tasks and seven of nine browser tasks reached terminal
results. The browser searches produced no qualifying Dublin candidate. Web
Search produced three validated observations: one duplicate was suppressed by
the canonical writer, leaving two unique strong-identity records. A newly
observed Teamtailor source entered the registry as `candidate`, not as an
automatically trusted source.

Each wave passed through `discovery_batch.py` and an isolated real canonical
merge. Wave 1 added one record, wave 2 added none but remained below the
diminishing-return stop, and wave 3 added one before stopping with
`plan_exhausted`. The temporary job table, evaluation snapshots, manifests,
metrics, and source registry were removed on exit. Production data was not
modified.

The aggregate evidence is in
[`performance/discovery-wave-complete-smoke-2026-09-21.json`](performance/discovery-wave-complete-smoke-2026-09-21.json).
It contains no CV, query, candidate, URL, title, company, JD, Cookie, or account
content. Because this run used a generic role/location probe rather than a user
CV, it validates discovery orchestration and merge integrity only; it does not
measure recall or prove that either job is CV-qualified.
