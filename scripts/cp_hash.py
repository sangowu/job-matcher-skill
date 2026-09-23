#!/usr/bin/env python3
"""算稳定的 candidate_profile_hash（cp_hash）。

candidate_profile 由编排者(LLM)产出，内容/键序/大小写/空白每轮可能波动，
直接 hash 会导致 match_score 缓存键每轮分裂（同一职位的历史评分取不到）。
本脚本先**规范化**再 hash —— 同语义的 profile 得到同 cp_hash。

编排者构造 candidate_profile 后，用本脚本算 cp_hash，再传给
merge_jobs.py / render_html.py 的 --cp-hash，保证全流程一致。

用法:  python cp_hash.py        (stdin: candidate_profile JSON)
输出:  {"ok": true, "cp_hash": "..."} | {"ok": false, "error": ...}
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from _stdio import StdinUnavailable, read_stdin_text


def normalize(obj):
    """递归规范化：dict 按键排序、list 元素规范化后排序、字符串去空白小写。"""
    if isinstance(obj, dict):
        return {k: normalize(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        items = [normalize(x) for x in obj]
        return sorted(items, key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
    if isinstance(obj, str):
        return obj.strip().lower()
    return obj


def _strip_fence(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        return text[s : e + 1]
    return text


def main() -> None:
    try:
        raw = read_stdin_text()
    except StdinUnavailable as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        sys.exit(1)
    if not raw.strip():
        print(json.dumps({"ok": False, "error": "stdin 为空"}))
        sys.exit(1)
    try:
        prof = json.loads(_strip_fence(raw))
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"JSON 解析失败：{e}"}))
        sys.exit(1)

    canon = json.dumps(normalize(prof), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    cp_hash = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    print(json.dumps({"ok": True, "cp_hash": cp_hash}))


if __name__ == "__main__":
    main()
