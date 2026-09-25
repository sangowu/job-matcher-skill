#!/usr/bin/env python3
"""模块6：渲染静态 HTML 报告。

读 jobs_table.json，按当前 cv_hash:cp_hash 取 match_score 展平职位，
注入 assets/template.html（占位符替换，零第三方依赖），同时嵌入 7/30 天
PII-safe 运行健康快照，输出自包含 HTML 并自动打开。

用法:
  python render_html.py --cv-hash H --cp-hash H [--meta-file F] [--no-open]

meta-file(可选 JSON): {profile_summary, new_count, cached_count, lang,
  report_language?, target_markets?, search_languages?, run_time?, route_summaries?}
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

from _jobutil import load_config, normalize_company
from runtime_metrics import DEFAULT_THRESHOLDS, build_summaries

SKILL_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_ROOT / "data"
TABLE_PATH = DATA_DIR / "jobs_table.json"
TEMPLATE_PATH = SKILL_ROOT / "assets" / "template.html"
REPORTS_DIR = DATA_DIR / "reports"
METRICS_PATH = DATA_DIR / "metrics.jsonl"
EVAL_RUNS_DIR = DATA_DIR / "eval_runs"
SUPPORTED_MARKETS = {"ie", "uk", "cn", "de"}
INTERNAL_LANGUAGES = {"en", "de", "zh-Hans"}
COVERAGE_STATUSES = {"executed", "partial", "failed", "skipped", "not_collected", "unknown"}


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


def _unique_strings(values: object, allowed: set[str] | None = None) -> list[str]:
    output: list[str] = []
    if not isinstance(values, list):
        return output
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if not normalized or (allowed is not None and normalized not in allowed):
            continue
        if normalized not in output:
            output.append(normalized)
    return output


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _coverage_from_routes(target_markets: list[str], rows: object) -> list[dict]:
    route_rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    coverage: list[dict] = []
    for market_id in target_markets:
        market_rows = [row for row in route_rows if row.get("market_id") == market_id]
        statuses = [row.get("status") for row in market_rows]
        if not market_rows:
            status = "not_collected"
        elif all(value == "skipped" for value in statuses):
            status = "skipped"
        elif all(value == "failed" for value in statuses):
            status = "failed"
        elif any(value in {"failed", "skipped"} for value in statuses):
            status = "partial"
        elif all(value == "succeeded" for value in statuses):
            status = "executed"
        else:
            status = "unknown"
        coverage.append(
            {
                "market_id": market_id,
                "status": status,
                "sources_planned": sum(_count(row.get("sources_planned")) for row in market_rows),
                "sources_succeeded": sum(_count(row.get("sources_succeeded")) for row in market_rows),
                "sources_failed": sum(_count(row.get("sources_failed")) for row in market_rows),
                "sources_skipped": sum(
                    _count(row.get("sources_planned"))
                    for row in market_rows
                    if row.get("status") == "skipped"
                ),
                "candidates_incremental": sum(
                    _count(row.get("candidates_incremental")) for row in market_rows
                ),
            }
        )
    return coverage


def _normalize_coverage(target_markets: list[str], meta: dict) -> list[dict]:
    explicit = meta.get("market_coverage")
    if not isinstance(explicit, list):
        return _coverage_from_routes(target_markets, meta.get("route_summaries"))
    by_market = {
        row.get("market_id"): row
        for row in explicit
        if isinstance(row, dict) and row.get("market_id") in SUPPORTED_MARKETS
    }
    output: list[dict] = []
    for market_id in target_markets:
        row = by_market.get(market_id, {})
        status = row.get("status") if isinstance(row, dict) else None
        output.append(
            {
                "market_id": market_id,
                "status": status if status in COVERAGE_STATUSES else "unknown",
                "sources_planned": _count(row.get("sources_planned")),
                "sources_succeeded": _count(row.get("sources_succeeded")),
                "sources_failed": _count(row.get("sources_failed")),
                "sources_skipped": _count(row.get("sources_skipped")),
                "candidates_incremental": _count(row.get("candidates_incremental")),
            }
        )
    return output


def normalize_report_meta(meta: object, *, now: datetime | None = None) -> dict:
    raw = dict(meta) if isinstance(meta, dict) else {}
    report_language = raw.get("report_language") or raw.get("lang") or "en"
    report_language = "zh" if report_language in {"zh", "zh-CN", "zh-Hans"} else "en"
    target_markets = _unique_strings(raw.get("target_markets"), SUPPORTED_MARKETS)
    if not target_markets:
        coverage_rows = raw.get("market_coverage") or raw.get("route_summaries")
        if isinstance(coverage_rows, list):
            target_markets = _unique_strings(
                [row.get("market_id") for row in coverage_rows if isinstance(row, dict)],
                SUPPORTED_MARKETS,
            )
    search_languages = _unique_strings(raw.get("search_languages"), INTERNAL_LANGUAGES)
    if not search_languages and isinstance(raw.get("route_summaries"), list):
        search_languages = _unique_strings(
            [
                row.get("search_language")
                for row in raw["route_summaries"]
                if isinstance(row, dict)
            ],
            INTERNAL_LANGUAGES,
        )
    generated = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    run_time = raw.get("run_time") or raw.get("generated_at") or generated
    if not isinstance(run_time, str) or len(run_time) > 80:
        run_time = generated
    coverage = _normalize_coverage(target_markets, raw)
    if any(row["status"] in {"failed", "partial"} for row in coverage):
        empty_state_reason = "source_failed"
    elif not coverage or any(
        row["status"] in {"not_collected", "skipped", "unknown"} for row in coverage
    ):
        empty_state_reason = "not_collected"
    else:
        empty_state_reason = "executed_zero"
    raw.update(
        {
            "lang": report_language,
            "report_language": report_language,
            "target_markets": target_markets,
            "search_languages": search_languages,
            "run_time": run_time,
            "market_coverage": coverage,
            "empty_state_reason": empty_state_reason,
        }
    )
    raw.pop("route_summaries", None)
    return raw


def _role_key(job: dict) -> str:
    """One company, one title, spelled the same way.

    Deliberately not the row's `dedup_key`. That key is a *weak* one, built for
    merging, where it is only ever used together with a location check and an
    identity check -- so `normalize_title` strips parentheticals and anything
    after a dash, and Intercom's "Senior Data Scientist - AI Tooling", "- Growth"
    and "(GTM)" all collapse onto "senior data scientist". Three different jobs.
    Used alone to claim two rows are the same posting it is far too coarse; here
    the titles have to actually match.
    """
    company = normalize_company(str(job.get("company") or ""))
    title = " ".join(str(job.get("title") or "").split()).lower()
    return f"{company}|{title}"


def same_role_postings(jobs: list) -> dict[int, list[dict]]:
    """For each job, the other rows advertising the same role.

    An employer sometimes opens more than one requisition for one job. MongoDB
    did on 2026-09-25: two Greenhouse postings of "Senior Software Engineer,
    Forward Deployed AI Engineer", 41 lines of description apart by eight
    characters, one saying "Ireland" and the other "Cork, Ireland; Dublin,
    Ireland".

    Merging them would be wrong -- they carry different `gh_jid`s and different
    apply URLs, and applying to one is not applying to the other, so `merge_jobs`
    is right to keep both. But nothing said they were the same role, so the
    report showed it twice with nothing connecting the two, and one of the
    Top-N slots went to a posting the reader had already considered.

    Keyed by position rather than by record id, because a row is not required
    to have one.
    """
    by_role: dict[str, list[int]] = {}
    for position, job in enumerate(jobs):
        if isinstance(job, dict):
            by_role.setdefault(_role_key(job), []).append(position)
    siblings: dict[int, list[dict]] = {}
    for positions in by_role.values():
        for position in positions:
            siblings[position] = [
                {
                    "location": str(jobs[other].get("location") or ""),
                    "url": _safe_url(str(jobs[other].get("url") or "")),
                    "status": str(jobs[other].get("status") or "existing"),
                }
                for other in positions
                if other != position
            ]
    return siblings


def flatten(job: dict, mk: str, *, same_role: list[dict] | None = None) -> dict:
    scores = job.get("match_scores") or {}
    # 只认当前 cv:cp 口径的评分。不回退其他 CV/求职意向的旧分：
    # 评分是 JD × CV × 意向的函数，跨口径展示会误导（stale_score 标记待重评）。
    ms = scores.get(mk) or {}
    stale = not ms and bool(scores)
    raw_sources = [source for source in job.get("raw_sources", []) if isinstance(source, dict)]
    provenance = []
    for source in raw_sources:
        normalized = source.get("location_normalized")
        if not isinstance(normalized, dict):
            normalized = {}
        provenance.append(
            {
                "source": str(source.get("source") or source.get("source_id") or "unknown"),
                "source_id": str(source.get("source_id") or "unknown"),
                "source_type": str(source.get("source_type") or "unknown"),
                "discovery_route": str(source.get("discovery_route") or "unknown"),
                "search_language": str(source.get("search_language") or "unknown"),
                "observed_at": source.get("observed_at"),
                "link_verification_status": str(
                    source.get("link_verification_status") or "unknown"
                ),
                "location_normalized": {
                    "market_id": normalized.get("market_id"),
                    "city_id": normalized.get("city_id"),
                    "remote_scope": normalized.get("remote_scope"),
                    "confidence": normalized.get("confidence") or "unknown",
                },
                "url": _safe_url(source.get("url", "")),
            }
        )
    market_ids = _unique_strings(job.get("market_ids"), SUPPORTED_MARKETS)
    if not market_ids:
        market_ids = _unique_strings(
            [row["location_normalized"].get("market_id") for row in provenance],
            SUPPORTED_MARKETS,
        )
    source_types = _unique_strings([row["source_type"] for row in provenance])
    discovery_routes = _unique_strings([row["discovery_route"] for row in provenance])
    verification_statuses = _unique_strings(
        [row["link_verification_status"] for row in provenance]
    )
    if not verification_statuses and job.get("verified"):
        verification_statuses = [str(job["verified"])]
    return {
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "url": _safe_url(job.get("url", "")),
        "salary": job.get("salary", ""),
        "date_posted": job.get("date_posted", ""),
        "first_seen": job.get("first_seen", ""),
        "status": job.get("status", "existing"),
        "sources": _unique_strings([row["source"] for row in provenance]),
        "source_urls": [
            {"source": row["source"], "url": row["url"]} for row in provenance
        ],
        "provenance": provenance,
        "source_count": len(provenance),
        "multi_source": len(provenance) > 1,
        "source_types": source_types or ["unknown"],
        "discovery_routes": discovery_routes or ["unknown"],
        "verification_statuses": verification_statuses or ["unknown"],
        "market_ids": market_ids,
        "market_status": "known" if market_ids else "unknown",
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
        # The other postings of this same role, so the reader sees one job
        # advertised twice rather than two jobs. Empty for all but a handful of
        # rows, and never a reason to drop one: each has its own apply URL.
        "same_role": list(same_role or []),
        "same_role_count": len(same_role or []),
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

    meta = normalize_report_meta(meta)
    lang = meta["lang"]

    mk = f"{args.cv_hash}:{args.cp_hash}"
    table_jobs = table.get("jobs", [])
    siblings = same_role_postings(table_jobs)
    jobs = [
        flatten(job, mk, same_role=siblings.get(position))
        for position, job in enumerate(table_jobs)
    ]
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
