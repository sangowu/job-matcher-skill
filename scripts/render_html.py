#!/usr/bin/env python3
"""模块6：渲染静态 HTML 报告。

读 jobs_table.json，按当前 cv_hash:cp_hash 取 match_score 展平职位，
注入 assets/template.html（占位符替换，零第三方依赖），同时嵌入 7/30 天
PII-safe 运行健康快照，输出自包含 HTML 并自动打开。

用法:
  python render_html.py --cv-hash H --cp-hash H [--meta-file F] [--no-open]

meta-file(可选 JSON): {profile_summary, new_count, cached_count, lang}
输出: {"ok": true, "report_path": "...", "job_count": N, "health_status": "..."}
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _jobutil import load_config
from runtime_metrics import DEFAULT_THRESHOLDS, build_summaries

SKILL_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_ROOT / "data"
TABLE_PATH = DATA_DIR / "jobs_table.json"
TEMPLATE_PATH = SKILL_ROOT / "assets" / "template.html"
REPORTS_DIR = DATA_DIR / "reports"
METRICS_PATH = DATA_DIR / "metrics.jsonl"
EVAL_RUNS_DIR = DATA_DIR / "eval_runs"


def _unavailable_summary(days: int, thresholds: dict) -> dict:
    generated_at = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "window": {
            "days": days,
            "since": (generated_at - timedelta(days=days)).isoformat(),
        },
        "status": "unavailable",
        "thresholds": thresholds,
        "breaches": [],
        "metrics": {},
    }


def build_health_payload() -> dict:
    """Build static monitoring snapshots without making report rendering depend on them."""
    config = load_config()
    configured = config.get("monitoring_thresholds")
    thresholds = {
        **DEFAULT_THRESHOLDS,
        **(configured if isinstance(configured, dict) else {}),
    }
    try:
        return build_summaries(
            METRICS_PATH,
            EVAL_RUNS_DIR,
            days=(7, 30),
            thresholds=thresholds,
        )
    except Exception:
        # Monitoring is best effort. Do not expose exception text or block the job report.
        return {
            f"{days}d": _unavailable_summary(days, thresholds)
            for days in (7, 30)
        }


def _embed_json(obj: object) -> str:
    """序列化为可安全内嵌 <script> 的 JSON：`</` 转义为 `<\\/`。

    职位 title/snippet 来自外部网页，可能含 `</script>`，不转义会提前终止
    内联脚本块，导致外部数据注入报告 HTML。`\\/` 是合法 JSON 转义，
    浏览器端 JSON 语义不变。
    """
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def _safe_url(url: str) -> str:
    """只放行 http/https 链接，拦截 javascript: 等可执行 scheme。"""
    url = (url or "").strip()
    if url.lower().startswith(("http://", "https://")):
        return url
    return ""


def flatten(job: dict, mk: str) -> dict:
    scores = job.get("match_scores") or {}
    # 只认当前 cv:cp 口径的评分。不回退其他 CV/求职意向的旧分：
    # 评分是 JD × CV × 意向的函数，跨口径展示会误导（stale_score 标记待重评）。
    ms = scores.get(mk) or {}
    stale = not ms and bool(scores)
    return {
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "url": _safe_url(job.get("url", "")),
        "salary": job.get("salary", ""),
        "date_posted": job.get("date_posted", ""),
        "first_seen": job.get("first_seen", ""),
        "status": job.get("status", "existing"),
        "sources": [rs.get("source", "") for rs in job.get("raw_sources", [])],
        "source_urls": [{"source": rs.get("source", ""), "url": _safe_url(rs.get("url", ""))}
                        for rs in job.get("raw_sources", [])],
        "possibly_closed": job.get("possibly_closed", False),
        "verified": job.get("verified"),
        "scored_from": job.get("scored_from"),
        "score": ms.get("overall_score"),
        "recommendation": ms.get("recommendation"),
        "strengths": ms.get("strengths", []),
        "weaknesses": ms.get("weaknesses", []),
        "matched_keywords": ms.get("matched_keywords", []),
        "title_score": ms.get("title_score"),
        "seniority_score": ms.get("seniority_score"),
        "skills_score": ms.get("skills_score"),
        "location_score": ms.get("location_score"),
        "must_have_score": ms.get("must_have_score"),
        "stale_score": stale,
        "jd": job.get("jd_profile") or {},
    }


def open_file(path: Path) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cv-hash", required=True)
    ap.add_argument("--cp-hash", required=True)
    ap.add_argument("--meta-file", default="")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    if not TABLE_PATH.exists():
        print(json.dumps({"ok": False, "error": "jobs_table.json 不存在，请先运行检索"}))
        sys.exit(1)
    table = json.loads(TABLE_PATH.read_text(encoding="utf-8"))

    meta = {}
    if args.meta_file and Path(args.meta_file).exists():
        try:
            meta = json.loads(Path(args.meta_file).read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    lang = meta.get("lang") or "en"
    if lang not in ("zh", "en"):
        lang = "en"

    mk = f"{args.cv_hash}:{args.cp_hash}"
    jobs = [flatten(j, mk) for j in table.get("jobs", [])]
    health = build_health_payload()

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = (template
            .replace("__JOBS_JSON__", _embed_json(jobs))
            .replace("__META_JSON__", _embed_json(meta))
            .replace("__HEALTH_JSON__", _embed_json(health))
            .replace("__LANG__", lang))

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = REPORTS_DIR / f"report_{ts}.html"
    out.write_text(html, encoding="utf-8")

    # 运行日志（每轮留痕，便于诊断 cp_hash 分裂、无分职位等问题）
    jobs_all = table.get("jobs", [])
    run_log = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "cv_hash": args.cv_hash, "cp_hash": args.cp_hash,
        "report_path": str(out), "job_count": len(jobs),
        "with_current_mk": sum(1 for j in jobs_all if (j.get("match_scores") or {}).get(mk)),
        "with_any_score": sum(1 for j in jobs_all if j.get("match_scores")),
        "no_score": sum(1 for j in jobs_all if not j.get("match_scores")),
        "new": sum(1 for j in jobs_all if j.get("status") == "new"),
    }
    try:
        with (DATA_DIR / "runs.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(run_log, ensure_ascii=False) + "\n")
    except Exception:
        pass

    if not args.no_open:
        open_file(out)

    current_health = health["7d"]
    print(json.dumps({"ok": True, "report_path": str(out), "job_count": len(jobs),
                      "opened": not args.no_open,
                      "health_status": current_health["status"],
                      "health_breaches": len(current_health["breaches"])}))


if __name__ == "__main__":
    main()
