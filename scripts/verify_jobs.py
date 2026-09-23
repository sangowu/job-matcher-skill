#!/usr/bin/env python3
"""模块4：失效职位验证（容错阶梯的状态码层）。

对一批 URL 查 HTTP 状态码 + 正文关闭关键词，判定职位是否仍有效。
是容错阶梯里最便宜的一层；更深的"已关闭但返回200"由 WebFetch/headless 兜底。

用法:  python verify_jobs.py   < urls.json    (urls.json = ["url1","url2",...])
输出:  {"results": [{url, alive, reason, final_url}]}
       alive: true(有效) / false(失效) / null(无法判定，不剔除)
"""
from __future__ import annotations

import json
import re
import sys

from _jobutil import is_closed_posting
from _stdio import StdinUnavailable, read_stdin_text

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_LISTING_TAIL = re.compile(r"/(jobs|careers|search|positions|opportunities)/?$", re.I)
_BODY_LIMIT = 8000


def check(url: str) -> dict:
    try:
        import requests
    except ImportError:
        return {"url": url, "alive": None, "reason": "requests 未安装", "final_url": url}

    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": _UA}, allow_redirects=True)
    except requests.exceptions.Timeout:
        return {"url": url, "alive": None, "reason": "timeout", "final_url": url}
    except Exception as e:
        return {"url": url, "alive": None, "reason": f"request error: {type(e).__name__}", "final_url": url}

    code = r.status_code
    final = r.url
    if code in (404, 410):
        return {"url": url, "alive": False, "reason": f"HTTP {code}", "final_url": final}
    if code >= 400:
        return {"url": url, "alive": False, "reason": f"HTTP {code}", "final_url": final}

    body = (r.text or "")[:_BODY_LIMIT]
    if is_closed_posting(body):
        return {"url": url, "alive": False, "reason": "正文含职位关闭关键词", "final_url": final}

    # 重定向到明显的列表页（job-id 丢失）→ 疑似失效
    if final != url and _LISTING_TAIL.search(final):
        return {"url": url, "alive": False, "reason": "重定向到列表页", "final_url": final}

    return {"url": url, "alive": True, "reason": "ok", "final_url": final}


def main() -> None:
    try:
        urls = json.loads(read_stdin_text() or "[]")
    except (StdinUnavailable, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "error": f"输入 JSON 解析失败：{e}"}))
        sys.exit(1)
    if not isinstance(urls, list):
        print(json.dumps({"ok": False, "error": "输入必须是 URL 数组"}))
        sys.exit(1)

    results = [check(u) for u in urls if isinstance(u, str) and u.strip()]
    print(json.dumps({"ok": True, "results": results}))


if __name__ == "__main__":
    main()
