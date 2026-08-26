# ATS Phase 3 data-quality audit

Date: 2026-08-26

Count-only artifact: [`ats-phase3-quality-audit.json`](ats-phase3-quality-audit.json)

## Outcome

The Phase 3 quality gate **failed**. The controlled ATS arm was mechanically correct but its emitted candidate set was not precise enough to merge as a completed optimization.

The same three-board, five-request setup was replayed without additional Web Search. It again emitted 20 candidates. An independent manual title-level review used these labels:

- `target_relevant`: the title directly names Applied AI, ML, LLM, AI evaluation, or agent-systems engineering.
- `adjacent_or_stretch`: software/backend/full-stack work with credible AI-system scope but not a direct target-role title.
- `false_positive`: the title is primarily mobile, Android, iOS, UI, or another non-target discipline and only mentions an AI product suffix.

## Results

| Metric | Fixed Web control | ATS emitted set |
|---|---:|---:|
| Candidates reviewed | 5 | 20 |
| Target-relevant | 4 | 5 |
| Adjacent/stretch | — | 3 |
| False positive | 1 | 12 |
| Strict precision | 80% | 25% |
| Precision including adjacent/stretch | — | 40% |
| Alive links | 5/5 | 20/20 |

Other ATS quality signals:

- All 20 records exposed description-available metadata and a posting date within 45 days, but description metadata is not equivalent to validating the JD content.
- All 20 emitted candidates came from one company and one Provider despite three successful boards.
- The set contained 15 exact titles; five records were excess exact-title repetitions with distinct Provider IDs.

## Root cause

The current title filter allows any non-generic token overlap. For a preferred role such as `Applied AI Engineer`, the remaining token `ai` is enough to accept titles whose actual discipline is mobile, Android, iOS, or UI when the suffix names an “AI Finance Agent” or “AI Neobank” product. The candidate cap is then filled without an independent relevance rank or company/title diversity guard.

The ATS candidates also do not hand JD content to evaluation workers, so the pipeline cannot use required skills or must-have requirements to correct these title-level false positives before emission.

## Gate decision

PR #21 should remain unmerged until the title matcher is tightened and the same frozen review is rerun. A reasonable next acceptance condition is:

- ATS strict precision at least 70% on this fixed 20-item audit, with no regression to Web-record preservation or strong-identity deduplication.
- No title passes solely because `ai` appears in a product/team suffix.
- Link-alive rate remains 100% for the reviewed set.

This audit does not claim full CV-to-JD match quality. That requires a later JD handoff and five-dimension scoring experiment.
