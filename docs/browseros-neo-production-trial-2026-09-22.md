# BrowserOS Neo full-CV production trial

On 2026-09-22, the production discovery path completed one authorized,
Dublin-first Ireland run with a real local CV, BrowserOS Neo, and model/Web
Search. This was the first full run to continue through discovery planning,
three canonical wave commits, JD evaluation, and the interactive HTML report;
it was not an isolated temporary-store smoke.

## Result

- The deterministic plan contained three waves and 12 tasks: nine browser
  source tasks and three Web Search tasks.
- BrowserOS Neo reached terminal outcomes for every assigned task: three
  succeeded, two failed safely, and four were skipped at an explicit policy or
  site boundary.
- All three Web Search tasks succeeded. Across all task results, 47 raw result
  rows produced two prefiltered and contract-valid candidates.
- The single-writer merge added two strong-identity records, created two
  evaluation tasks, and stopped with `plan_exhausted` after wave 3.
- Both candidates were verified from live JD pages, scored from JD content,
  and rendered in the report. The report contained two stretch applications;
  it did not present either role as a safe match.
- Targeted report verification passed 16 tests. The full repository suite run
  immediately before the implementation commit passed 385 tests.

The count-only artifact is
[`performance/browseros-neo-production-trial-2026-09-22.json`](performance/browseros-neo-production-trial-2026-09-22.json).
The generated HTML and production data remain under ignored `data/` paths and
are not committed.

## Observed compatibility boundaries

The run deliberately failed closed rather than converting site friction into
false coverage:

- One public board required sign-in before search results could be read.
- One public-sector source redirected to a legitimate ATS host that was not in
  the task's original host boundary.
- Two consent banners used accessibility containers outside the classifier's
  currently accepted `dialog`/`alertdialog` boundary, so no consent button was
  clicked.
- Two company portals exposed readable results or controls, but their custom
  comboboxes did not react to semantic fill/type actions.
- Three browser sources completed successfully but produced no qualifying
  candidate for this CV. Both accepted candidates came from the Web Search
  route in this run.

These outcomes are source-access evidence, not code-defect counts and not a
market-recall estimate. A later run may observe different layouts or vacancies.

## Metrics limitation

BrowserOS Neo MCP actions were executed directly by the Agent. They did not
emit the repository's `browser` runtime-metric events, which are currently
written by the separate remote-browser adapter. `round_timer.py` therefore
finished the run with `metrics_status=incomplete` and
`missing_operations=browser`; the generated report correctly displayed
`health_status=unknown` rather than claiming healthy coverage.

This is an instrumentation gap, not a discovery, merge, evaluation, or render
failure. A provider-neutral local-browser metric adapter remains required
before Neo-backed production runs can satisfy the browser completeness gate.

## Privacy boundary

- Browser work used dedicated Agent-owned tabs and did not export cookies,
  credentials, history, browser profiles, or session identifiers.
- Repository evidence contains no CV text, query, job title, company, URL, JD,
  account metadata, or generated report.
- Temporary search requests, candidate-profile input, wave payloads, evaluation
  payloads, and report metadata were deleted after the run.
- No application, message, upload, save, follow, or account mutation occurred.

## Gate update

This trial closes the earlier gate for a full single-market CV production run
through the canonical table and HTML report. It does not close cross-site host
alias handling, ambiguous consent handoff, custom-combobox compatibility,
additional-page pagination, CAPTCHA recovery, rate-limit recovery, or local
browser metric completeness.
