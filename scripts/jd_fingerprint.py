#!/usr/bin/env python3
"""A fingerprint of a JD's wording, so one posting carried by several publishers
can be recognized without keeping the text.

Recruitment agencies repost a client's role under their own name. A live
Singapore round (2026-10-14) kept "AI Engineer" from Tap Growth ai and twice
from Talent-Merge: the same text, the same salary band, the same recruiter
registration number. `dedup_key` is company + title, so they were three rows
and three of the report's places. Two of the texts were identical; the third
was the same words laid out as bullets with one paragraph repeated.

The fingerprint is a 64-bit SimHash over word 5-shingles: near-identical texts
land a few bits apart and unrelated ones about half the bits apart. Measured on
ten of that round's Singapore JDs, the three reposts were 0 and 5 bits apart
and every other pair 28 or more; `NEAR_DUPLICATE_BITS` sits between. The text
itself is never stored -- only these 16 hex characters, which cannot be turned
back into it.

Usage:  python jd_fingerprint.py   (stdin: the JD text)
Output: {"ok": true, "jd_fingerprint": "<16 hex>"} -- empty when the text is
        too short to say anything.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys

from _stdio import StdinUnavailable, read_stdin_text, use_utf8_stdout

SHINGLE_WORDS = 5
NEAR_DUPLICATE_BITS = 8
_BITS = 64
_FINGERPRINT = re.compile(r"^[0-9a-f]{16}$")


def jd_fingerprint(text: str) -> str:
    words = re.findall(r"\w+", (text or "").casefold())
    shingles = {
        " ".join(words[i:i + SHINGLE_WORDS])
        for i in range(len(words) - SHINGLE_WORDS + 1)
    }
    if not shingles:
        return ""
    weights = [0] * _BITS
    for shingle in shingles:
        value = int.from_bytes(
            hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest(), "big"
        )
        for bit in range(_BITS):
            weights[bit] += 1 if value >> bit & 1 else -1
    return f"{sum(1 << bit for bit in range(_BITS) if weights[bit] > 0):016x}"


def is_fingerprint(value: object) -> bool:
    return isinstance(value, str) and bool(_FINGERPRINT.fullmatch(value))


def distance(left: str, right: str) -> int:
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def near_duplicates(left: object, right: object) -> bool:
    return (
        is_fingerprint(left)
        and is_fingerprint(right)
        and distance(left, right) <= NEAR_DUPLICATE_BITS
    )


def main() -> None:
    use_utf8_stdout()
    try:
        text = read_stdin_text()
    except StdinUnavailable as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps({"ok": True, "jd_fingerprint": jd_fingerprint(text)}))


if __name__ == "__main__":
    main()
