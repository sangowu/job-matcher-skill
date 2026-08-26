# ATS Phase 4 JD handoff benchmark

Date: 2026-08-26. Platform: Windows 11, Python 3.13.5. The run used 3 warmups and 30 measured iterations with no Web Search, network, or cloud-provider calls. Raw count-only evidence: [`ats-phase4-jd-handoff.json`](ats-phase4-jd-handoff.json).

## Result

| Metric | p50 | p95 |
|---|---:|---:|
| Three-provider Fake ATS normalization | 4.094 ms | 5.108 ms |
| ATS normalization + merge + JD task handoff | 7.408 ms | 8.367 ms |
| Existing 15-job core pipeline | 41.675 ms | 46.376 ms |

Each measured ATS iteration processed 3 boards / 3 requests / 3 emitted jobs. All 3 emitted jobs carried JD text into their run-scoped evaluation tasks, all 3 were eligible to avoid a later browser fetch, and the canonical table contained 0 raw JDs. The benchmark also asserts this privacy boundary on every iteration.

Compared with the earlier v2.3.0 local browser-provider artifact, the unchanged core fixture was 31.3% lower at p50 and 28.2% lower at p95. This is a cross-run observation on the same machine, not proof that Phase 4 caused a core speedup. There is no equivalent pre-Phase-4 JD-handoff latency, so 7.408/8.367 ms is the first comparable baseline for that path.

## Scope

This benchmark proves deterministic normalization, transient handoff, count-only observability, and zero raw JD text in the canonical table. Its handoff timer runs merge in-process and excludes CLI subprocess startup. It does not prove live ATS latency, full CV-to-JD scoring quality, or that a browser was actually invoked before Phase 4. Live validation therefore reports “eligible fetches avoided” and separately audits five-dimensional analysis quality.
