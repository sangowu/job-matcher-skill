# Multi-region Phase B source registry

Phase B adds a public source catalog and a local health registry. It does not
execute sources, emit job candidates, change the canonical job table, or alter
the existing Web Search/ATS/report path.

## Public seeds

`references/source_seeds.json` contains 23 manually checked public entry points:

- Ireland: IrishJobs, Jobs.ie, and publicjobs;
- United Kingdom: Reed, CV-Library, and jobs.ac.uk;
- China: BOSS Zhipin, Liepin, Lagou, and Nankai University's public campus-job board;
- Germany: Bundesagentur für Arbeit, StepStone, and Make it in Germany;
- ten global company career portals shared by all four markets.

Each market therefore has at least three local sources plus ten company/ATS
sources. BOSS Zhipin, Liepin, and Lagou remain `web_search`/`manual_browser`
only. The Nankai board has a separately verified public read-only listing and
detail path; it is campus-focused and does not establish broad China-market
coverage. Login, captcha, and site access controls must stop automation. The
seed file contains no credentials, cookies, CV data, queries, or personal data.

Validate the catalog and its fixed coverage threshold:

```text
python scripts/source_registry.py validate
```

## Runtime registry

Initialize `data/source_registry.json` with an exclusive lock and atomic file
replacement:

```text
python scripts/source_registry.py init
```

The runtime registry stores source IDs, routing metadata, state, health
timestamps, TTL, and bounded failure counters. It deliberately does not copy
seed URLs and rejects job titles, job URLs, queries, JD text, CV text, email, or
phone fields. New agent proposals start as `candidate` and disabled. A
successful verification changes health state but does not auto-enable a newly
discovered source.

Workers return an immutable batch; only the main orchestrator applies it:

```json
{
  "batch_id": "source-check-001",
  "proposals": [],
  "events": [
    {"source_id": "amazon-careers", "outcome": "verified"}
  ]
}
```

```text
python scripts/source_registry.py apply < source_batch.json
```

Repeated `batch_id` values are no-ops. `timeout`, `rate_limited`, and
`network_error` remain retryable. Three consecutive `not_found`/`gone` results
mark a source unavailable; a later `verified` event restores it. `disabled` is
only entered through the explicit `disable` event.

The deterministic inspection plan selects only enabled, verified, unexpired
sources. A source covering several requested markets appears once:

```text
python scripts/source_registry.py plan --markets ie uk
```

## Legacy ATS migration and rollback

On first initialization, an existing `data/ats_companies.json` is parsed before
any write, imported into the generic registry in one atomic commit, and marked
as `ats_companies_v1`. The legacy file remains untouched and available to the
existing ATS pipeline for one compatibility release. After migration,
`source_registry.py` writes only the generic registry.

If a legacy row lacks region metadata, it receives no market rather than being
guessed into all four markets. Corrupt legacy JSON aborts initialization before
the generic registry changes.

Rollback removes only source IDs recorded by the migration marker and leaves
both seed rows and the legacy file intact:

```text
python scripts/source_registry.py rollback-legacy
```

The rolled-back marker prevents an automatic re-import on the next `init`.
During that rollback state, the existing ATS pipeline can read the legacy file
as a compatibility view but does not write health changes back to either file.
Closing `multi_region_enabled` also remains a non-destructive runtime rollback:
it does not delete either registry or change existing jobs.
