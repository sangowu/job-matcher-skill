# Local-browser Phase 2: runtime adapter contract

Phase 2 connects the discovery selector to browser tools already exposed to the
current Agent. It deliberately does not implement an OS-level detector or an
MCP client: public skills cannot assume one host, one browser install path, or
one tool namespace.

## Capability probe

The Agent first inspects its own available tools. It then sends a small
attestation to `python scripts/local_browser_probe.py probe` on stdin:

```json
{
  "provider": "browseros_neo",
  "connected": true,
  "authorized": true,
  "tools": [
    "mcp__browserclaw__tabs",
    "mcp__browserclaw__navigate",
    "mcp__browserclaw__read"
  ]
}
```

The probe accepts `browseros_neo` and `user_browser`. Provider-specific tool
names may be supplied in `tools`; runtimes with a bundled browser API may map
their surface to the canonical `operations` values `tabs`, `navigate`, and
`read`. A route is `ready` only when it is connected, explicitly authorized,
and has all three operations. The output never echoes tool names.

Do not scan processes or ports, read BrowserOS runtime files, attach to a raw
browser profile, or inspect cookies to manufacture this attestation. Tool
presence is the initial signal; the first real browser call remains the live
connection check. If that call fails, submit `connection_lost` to
`discovery_mode.py event` and follow its bounded fallback.

## Route order

Pass both browser probe statuses plus the current model-search status to
`discovery_mode.py plan`.

- `coverage`: use the preferred browser provider and model search independently;
  this is the repository default for broader source coverage.
- `auto`: preserve the legacy single-route order: BrowserOS Neo, else an
  authorized user browser, else model/Web Search.
- `combined`: run the preferred browser route and model search independently.
- `browser_only` and `model_only`: preserve the explicit restriction.

## Browser discovery boundary

For each browser search task:

1. Open a dedicated Agent tab. Do not enumerate, read, or reuse unrelated
   existing tabs.
2. Navigate only to the `entry_url` and same-site redirects authorized by the
   current DiscoveryPlan task. Locate search controls from the current
   accessibility tree; do not depend on published CSS selectors or coordinates.
   Use read-only page extraction (`read`, `snapshot`, or an equivalent) for
   discovery.
3. Do not submit applications, send messages, change account settings, upload
   files, execute page scripts, or export cookies. Account state is reused only
   because the browser itself already has the user's login session.
4. Extract a bounded CandidateEnvelope batch. Set `discovery_route` to
   `browseros_neo` or `user_browser`; keep `source_type` as the actual source
   type. Cross-market job platforms use `global_job_board`, not
   `local_job_board`. Restrict reads to the job list/detail subtree so account
   navigation, notification counts, and personalized sidebars never enter a
   candidate or log. Use `link_verification_status: "alive"` only after opening the job
   detail page and observing an active job/application path; listing-only
   candidates remain `unknown`.
5. Validate the batch with `candidate_contract.py`, then let the orchestrator
   pass it to the existing `merge_jobs.py merge` single writer. Never create a
   second browser-only job table.
6. Close only the dedicated Agent tab when the task finishes.

If a site presents login, CAPTCHA, consent requiring judgment, or rate
limiting, stop that site and emit `user_action_required` or `rate_limited`.
Keep unrelated routes running, visibly notify the user, and resume only after
the user completes the action. Never bypass a challenge or silently switch
browsers for the same blocked site.

## Current limit

This phase provides a portable runtime adapter contract and canonical data
handoff. The HTML settings/status and flashing human-action alert are added in
`local-browser-phase3-panel.md`. A bounded real-device Neo MCP
create/navigate/read/close smoke is recorded in
`local-browser-phase4-smoke.md`; the same evidence now includes a real
job-board login pause, panel resume, authenticated landing, and bounded
same-tab search. Phase 4 also records one scoped live candidate through detail
verification, CandidateEnvelope validation, and temporary-store merge.
Additional-page/site/language coverage, a full production-table run,
CAPTCHA/consent recovery, and rate-limit recovery remain separate gates and
must not be claimed from unit or panel tests alone.
