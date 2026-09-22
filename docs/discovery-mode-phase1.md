# Discovery mode Phase 1: capability selection and safe fallback

This phase adds a deterministic, provider-neutral route decision. It does not
connect to BrowserOS Neo or attach to an existing browser by itself. Phase 2
adds the runtime tool-surface adapter contract described in
`local-browser-phase2.md` without changing the selector's privacy boundary.

## Capability observation

The orchestrating agent observes capabilities using its own runtime tools. It
passes only a status for each capability to `discovery_mode.py`; the script does
not inspect processes, browser profiles, cookies, ports, or credentials.

`ready` means that the route is connected, authorized by the user, and actually
executable in this agent runtime. Merely finding a BrowserOS Neo installation
does not make `browseros_neo` ready. The Phase 2 probe additionally requires a
connected, authorized `tabs + navigate + read` tool surface. Missing keys
default to `unknown` and fail closed.

Accepted statuses are `ready`, `needs_setup`, `unavailable`, `unsupported`, and
`unknown`. Never send URLs, CV/JD content, Cookie data, or account identifiers
to the decision script.

## Mode contract

| Mode | Route selection |
|---|---|
| `coverage` (default) | Select the preferred browser provider and model search independently; use both when ready and report degraded coverage when one is missing. |
| `auto` | Backward-compatible single route: BrowserOS Neo first, then an authorized user browser, then model search with `degraded` status. |
| `model_only` | Use only model/Web Search, even when a browser is ready. |
| `browser_only` | Use only a ready local browser. If none is ready, stop with `needs_setup` or `unavailable`; never silently use model search. |
| `combined` | Use the preferred ready local browser and model search; if one is missing, use the remaining route but report `degraded`. |

Example input to `python scripts/discovery_mode.py plan` on stdin:

```json
{"mode":"coverage","capabilities":{"browseros_neo":"unavailable","user_browser":"unavailable","model_search":"ready"}}
```

The output contains only `ok`, `mode`, `status`, `browser_provider`, `routes`,
and low-cardinality `issues`. If `ok` is false, do not run discovery or present
an empty report as a complete search.

## Mid-run browser events

`python scripts/discovery_mode.py event` accepts the same `mode` and capability
statuses, plus `active_browser` and `event`:

- `user_action_required` and `rate_limited` return `pause_site`; keep other site
  tasks running, notify the user, and do not automatically switch browsers or
  bypass a challenge.
- `connection_lost` marks only that browser unavailable and replans. `coverage`,
  `auto`, or `combined` may switch to another already authorized local browser
  or continue model search. `browser_only` never falls back to model search.

Route selection answers which capabilities may run. It does not choose sites.
After source-health planning, `discovery_plan.py` joins the public source catalog
and emits the bounded site/query tasks described in `discovery-plan.md`.

This selector has no background service and sends no notification itself. The
browser adapter and a live control panel are separate later phases; a static
HTML report cannot provide live handoff status.

## Verification and next gate

Offline tests cover default coverage routing, Neo preference, fallback to user
browser and model search, strict explicit modes, malformed status rejection,
connection loss, and user-action/rate-limit pauses. The Phase 2 runtime probe and handoff contract
are now present, but a real-device smoke is still required before claiming a
specific Neo installation is verified.
