# ATS HTTP compression A/B

## Outcome

The production ATS client now requests `gzip` responses by default. A bounded live A/B on 2026-08-27 passed the pre-registered equivalence and transfer gates.

| Metric | Baseline: identity | Optimized: gzip | Change |
|---|---:|---:|---:|
| Runs | 3 | 3 | — |
| Public GET requests per run | 3 | 3 | 0 |
| Normalized jobs per run | 292 | 292 | 0 |
| Jobs with JD text per run | 292 | 292 | 0 |
| Wire bytes p50 | 4,581,392 | 948,090 | -79.31% |
| Wall time p50 | 2,407.832 ms | 2,278.456 ms | -5.37% |

All three paired comparisons produced identical normalized-content fingerprints. Every run succeeded, request counts and job counts were equal, and the wire-byte reduction exceeded the 20% acceptance threshold.

## Design

- Three public boards, one each for Ashby, Greenhouse, and Lever.
- Three paired runs with alternating `AB/BA` order to reduce order bias.
- Both arms used the same board list, page size, one-page limit, three-request budget, three-worker cap, and production parser.
- The control arm did not advertise compression; the optimized arm sent `Accept-Encoding: gzip`.
- The committed JSON is count-only. Company names, board tokens, titles, URLs, descriptions, profile fields, and content fingerprints are excluded.

Reproduce with:

```text
python scripts/benchmark_ats_compression.py --output <json> --pairs 3 --max-workers 3 --page-size 50 --max-pages 1 --timeout-seconds 30
```

## Interpretation and limits

The result demonstrates a stable reduction in transferred response bytes for this bounded sample without changing parsed output. The observed wall-time reduction is reported, but is not treated as a guaranteed latency improvement because public-network variance remains uncontrolled. Lever did not compress its sampled response, while Ashby and Greenhouse did; the client remains compatible with uncompressed responses.

This benchmark does not measure model tokens, Web Search calls, provider monetary charges, candidate precision, or market-wide coverage. The machine-readable evidence is in [`ats-http-compression-ab.json`](ats-http-compression-ab.json).
