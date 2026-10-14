#!/usr/bin/env python3
"""Choose which coarse-ranked candidates get a full JD read and a precise score.

The precise stage reads `top_n + precise_buffer` JDs. Taken as one global top
by coarse score, a round over several markets spent them on whichever market
had the most strongly titled postings: a live Singapore/Madrid/Milan round
(2026-10-14) gave 18 of 20 to Singapore's "AI Engineer" listings, and Spain
and Italy went into the report scored from their titles alone. Discovery had
the same shape and was fixed by giving each market a share (`[R4-23]`); this is
that rule at the scoring stage.

Each target market is first offered an equal share of the slots, taken in turns
by rank, from its own best candidates; a market with fewer candidates than its
share leaves the rest unused, and every slot still free goes to the best of
what is left, whatever its market. A candidate recommended `skip` at the coarse
stage -- a hard constraint already failed -- never takes a reserved slot and is
read only when nothing else is left: a share exists to look at a market's best,
not to fill a quota. One market (or none) is a plain top by score, as before.

Usage:  python precise_pick.py   (stdin)
  {"target_markets": ["sg", "es", "it"],
   "limit": 20,                               # optional; top_n + precise_buffer
   "candidates": [{"record_id": "...", "market_ids": ["sg"],
                   "coarse_score": 71, "recommendation": "apply"}]}
Output: {"ok": true, "limit", "share_per_market", "picked": [record_id, ...],
         "picked_by_market": {...}, "candidates_by_market": {...}}
"""
from __future__ import annotations

import json
import math
import sys
from typing import Any

from _jobutil import load_config
from _stdio import StdinUnavailable, read_stdin_text, use_utf8_stdout

OTHER_MARKET = "other"


class PrecisePickError(ValueError):
    """The input cannot be ranked."""


def _limit(payload: dict[str, Any], config: dict[str, Any]) -> int:
    value = payload.get("limit")
    if value is None:
        value = int(config.get("top_n", 15)) + int(config.get("precise_buffer", 5))
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PrecisePickError("limit must be a non-negative integer")
    return value


def _candidates(payload: dict[str, Any], markets: list[str]) -> list[dict[str, Any]]:
    rows = payload.get("candidates")
    if not isinstance(rows, list):
        raise PrecisePickError("candidates must be an array")
    seen: set[str] = set()
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise PrecisePickError("each candidate must be an object")
        record_id = str(row.get("record_id") or "").strip()
        if not record_id or record_id in seen:
            raise PrecisePickError("each candidate needs a unique record_id")
        seen.add(record_id)
        score = row.get("coarse_score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise PrecisePickError(f"{record_id}: coarse_score must be numeric")
        market_ids = row.get("market_ids") or []
        if not isinstance(market_ids, list):
            raise PrecisePickError(f"{record_id}: market_ids must be an array")
        # A posting listed in several target markets counts once, for the
        # first of them in the round's own order.
        home = next((m for m in markets if m in market_ids), OTHER_MARKET)
        out.append({
            "record_id": record_id,
            "score": float(score),
            "market": home,
            "skip": str(row.get("recommendation") or "").strip().lower() == "skip",
        })
    out.sort(key=lambda row: (row["skip"], -row["score"], row["record_id"]))
    return out


def pick(payload: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PrecisePickError("input must be a JSON object")
    markets = list(dict.fromkeys(str(m) for m in payload.get("target_markets") or []))
    limit = _limit(payload, load_config() if config is None else config)
    ranked = _candidates(payload, markets)

    picked: list[dict[str, Any]] = []
    share = None
    if len(markets) > 1:
        share = math.ceil(limit / len(markets))
        queues = {
            market: [row for row in ranked if row["market"] == market and not row["skip"]][:share]
            for market in markets
        }
        for rank in range(share):
            for market in markets:
                if len(picked) < limit and rank < len(queues[market]):
                    picked.append(queues[market][rank])
    chosen = {row["record_id"] for row in picked}
    for row in ranked:
        if len(picked) >= limit:
            break
        if row["record_id"] not in chosen:
            picked.append(row)
            chosen.add(row["record_id"])
    picked.sort(key=lambda row: (-row["score"], row["record_id"]))

    def count(rows: list[dict[str, Any]]) -> dict[str, int]:
        counts = {market: 0 for market in markets}
        for row in rows:
            counts[row["market"]] = counts.get(row["market"], 0) + 1
        return counts

    return {
        "ok": True,
        "limit": limit,
        "share_per_market": share,
        "picked": [row["record_id"] for row in picked],
        "picked_by_market": count(picked),
        "candidates_by_market": count(ranked),
    }


def main() -> None:
    use_utf8_stdout()
    try:
        payload = json.loads(read_stdin_text() or "{}")
        result = pick(payload)
    except (StdinUnavailable, json.JSONDecodeError, PrecisePickError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
