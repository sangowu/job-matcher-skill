# DiscoveryPlan execution contract

`scripts/discovery_plan.py` turns three already-validated control-plane plans
into bounded execution tasks. It is read-only and performs no Web Search,
browser action, ATS request, or canonical-table write.

## Inputs

The stdin object contains:

- `market_plan`: output from `market_plan.py plan`, including the target
  markets and localized search rows;
- `source_plan`: output from `source_registry.py plan`, containing only
  eligible source IDs, markets, and priorities;
- `route_plan`: output from `discovery_mode.py plan`, including the selected
  browser provider and discovery routes.
- optional `browser_settings`: the safe output of `local_browser_panel.py
  settings`; its Cookie policy overrides the repository default for this plan.

The public `references/source_seeds.json` catalog supplies entry URLs, source
types, languages, access methods, and automation policy. The persisted runtime
health registry deliberately remains URL-free. The planner joins both views in
memory and never writes the resulting execution URLs to the health registry.

## Outputs

The deterministic plan contains three task lists and bounded execution waves:

- `browser`: one bounded semantic site-search task per selected source and
  market. Sources are selected across local, public-sector, global-board, and
  company-career categories before remaining capacity is filled by priority.
- `web_search`: the existing localized query rows, each with one-call scope and
  bounded public source hints. A source may be a Web hint even when its policy
  prohibits browser automation.
- `structured`: verified public endpoint/API sources when `ats_enabled` is
  explicitly true.

`waves` assigns every task exactly once. Wave 1 combines the highest-value
available channels and prioritizes browser-source category diversity. Later
waves contain deterministic overflow from the same verified plan. The Agent
executes only `initial_wave_id`; it must not speculate about or pre-run later
waves.

Browser tasks contain no CSS selectors or coordinates. They provide an HTTPS
entry URL, exact initial host, semantic query values, page limits, browser
provider, and stop conditions. The browser Agent must inspect the current
accessibility tree and stay within the dedicated-tab rules in
`local-browser-phase2.md`. Each task also carries the effective Cookie policy
and fail-closed semantic classifier contract from `cookie-consent.md`.

Every task returns CandidateEnvelope records only. All routes converge on the
existing `merge_jobs.py` single writer; the planner never creates a separate
browser or Web result store.

## Task result handoff

After the Agent executes the current wave, pass its `wave_id`, the original
plan, and one result for every task in that wave to `scripts/discovery_batch.py`.
A result contains only its
`task_id`, terminal `status`, low-cardinality `failure_kind`, funnel counts,
and CandidateEnvelope array.

`succeeded`, `failed`, and `skipped` are the only terminal statuses. A login,
CAPTCHA, rate limit, or consent decision pauses that site and is not a terminal
result until the user resumes or the task is explicitly skipped. Failed or
skipped results cannot contain candidates.

The batch handoff validates candidate route, source, market, and language
against the originating task. It then performs one canonical merge followed by
one source-health commit. Its content-free manifest stores hashes and count
summaries only, so a retry is idempotent without persisting a CV, query, URL,
job title, company, snippet, or JD.

The returned `continuation` decision uses the canonical merge's new-candidate
count, count-only prior progress, plan-owned remaining waves, and the existing
`stop_threshold`/`consecutive_empty_stop` settings. Only `continue` exposes the
next wave ID and task IDs. Callers cannot override remaining-work availability
for a wave-aware plan. Legacy plans without `waves` retain the previous
count-only compatibility field. This controls discovery expansion only; it is
not a claim that those candidates have complete JDs or passed CV matching.

## Failure boundaries

- A source absent from the current health plan cannot become a task.
- `automation_allowed=false`, a missing `public_read_only_page` method, an
  unsupported language, or an invalid URL excludes that source from browser
  execution.
- Login, CAPTCHA, rate limiting, or consent requiring judgment pauses only that
  site. Other planned tasks continue.
- If one discovery channel is unavailable, the plan may remain usable but the
  route selector reports degraded coverage.

The default source and query budgets live in `config.json`. Increasing them
changes coverage and cost; it does not relax source policy or browser privacy
rules.

The initial pause-boundary smoke and the completed three-wave, count-only run
are documented in
[`discovery-wave-live-smoke.md`](discovery-wave-live-smoke.md).
