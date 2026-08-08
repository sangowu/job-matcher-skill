"""共享工具：职位归一化 / 去重键 / URL 规范化 / 失效关键词 / 配置。

被 merge_jobs.py 和 verify_jobs.py 复用。零第三方依赖。
归一化与失效检测逻辑移植自 JobRadar jobradar/schemas.py。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

SKILL_ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    try:
        return json.loads((SKILL_ROOT / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── 公司/职位归一化 + 去重键（移植自 schemas.py）─────────────────────────────────
_LEGAL_SUFFIXES = re.compile(
    r",?\s*\b(llc|inc|ltd|co|corp|group|gmbh|ag|sa|sas|bv|nv|plc)\.?(?=\s|$)", re.IGNORECASE
)


def normalize_company(name: str) -> str:
    name = _LEGAL_SUFFIXES.sub("", name or "")
    return re.sub(r"\s+", " ", name).strip().lower()


def normalize_title(title: str) -> str:
    title = re.sub(r"\(.*?\)", "", title or "")        # 去括号内容
    title = re.sub(r"\s*[-–|].*$", "", title)          # 去 "- xxx" / "| xxx" 后缀
    return re.sub(r"\s+", " ", title).strip().lower()


def make_dedup_key(company: str, title: str) -> str:
    return f"{normalize_company(company)}|{normalize_title(title)}"


# ── 失效职位关键词（移植自 schemas._CLOSED_PATTERN，按语言分组便于扩展）─────────────
# 关键词只覆盖已收录语言的确定性信号；其他语言/模糊表述由精排 LLM 兜底判断
# （见 references/scoring_rubric.md「失效判断」）。新增语言时追加一个分组即可。
_CLOSED_PATTERNS: list[re.Pattern] = [
    # en
    re.compile(
        r"\b("
        r"applications?\s+(are\s+)?(now\s+)?(closed|ended|no longer accepted)"
        r"|no longer (accepting|available|open)"
        r"|position (has been |is )?(filled|closed|removed)"
        r"|this (job|position|vacancy|role) (is|has been|has) (closed|expired|filled|removed)"
        r"|job (is\s+)?no longer available"
        r"|vacancy (is\s+)?(closed|filled)"
        r"|(posting|listing|advert|advertisement)\s+(has\s+)?(expired|been removed)"
        r"|expired on indeed"
        r"|this exact role may not be open"
        r"|posting is to advertise potential job opportunities"
        r")\b",
        re.IGNORECASE,
    ),
    # zh（中文无词边界，不用 \b）
    re.compile(
        r"该职位已(关闭|下线|结束|停止招聘)"
        r"|职位已(关闭|下线|失效)"
        r"|停止招聘"
    ),
]


def is_closed_posting(text: str) -> bool:
    return bool(text) and any(pattern.search(text) for pattern in _CLOSED_PATTERNS)


# ── URL 规范化 → url_key（缓存命中键）────────────────────────────────────────────
# 主流招聘平台的 job-id 提取规则：命中即作为精确主键。
# 覆盖国际 ATS + 各区域主流招聘站（中国/澳新/英国），保证多地区搜索下
# 同一职位的不同落地 URL 能强命中同一个 url_key。
_PLATFORM_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"greenhouse\.io/[^/]+/jobs/(\d+)", re.I)),
    ("lever", re.compile(r"lever\.co/[^/]+/([0-9a-f]{8}-[0-9a-f-]{20,})", re.I)),
    ("linkedin", re.compile(r"linkedin\.com/jobs/view/(\d+)", re.I)),
    ("ashby", re.compile(r"ashbyhq\.com/[^/]+/([0-9a-f]{8}-[0-9a-f-]{20,})", re.I)),
    ("workday", re.compile(r"myworkdayjobs\.com/.+/job/[^/]+/[^/]*?_(R-?\d+)", re.I)),
    ("liepin", re.compile(r"liepin\.com/(?:[a-z]+/)?job/(\d+)", re.I)),
    ("zhipin", re.compile(r"zhipin\.com/job_detail/([0-9a-z~_-]+)\.html", re.I)),
    ("lagou", re.compile(r"lagou\.com/(?:wn/)?jobs/(\d+)", re.I)),
    ("seek", re.compile(r"seek\.(?:com\.au|co\.nz)/job/(\d+)", re.I)),
    ("reed", re.compile(r"reed\.co\.uk/jobs/[^/]+/(\d+)", re.I)),
]

# job-id 类参数：规范化时保留（小写比较）
_KEEP_PARAMS = {"jk", "jobid", "gh_jid", "currentjobid", "vjk"}
# 追踪类参数：丢弃（凡 utm_* 也丢）
_DROP_PARAMS = {"ref", "src", "gh_src", "fbclid", "gclid", "referrer", "trk", "trackingid"}


def canonicalize_url(url: str) -> str:
    """把 URL 规范化成稳定的 url_key，吸收追踪参数/锚点/www 差异。

    主流平台优先提 `平台:id`；否则用 host+path + 仅保留 job-id 参数。
    """
    url = (url or "").strip()
    if not url:
        return ""

    for platform, pat in _PLATFORM_PATTERNS:
        m = pat.search(url)
        if m:
            return f"{platform}:{m.group(1)}"

    # indeed 的 jk 参数单独处理（兼容多种域名）
    m = re.search(r"[?&]jk=([0-9a-f]+)", url, re.I)
    if m and "indeed" in url.lower():
        return f"indeed:{m.group(1)}"

    try:
        p = urlparse(url)
    except Exception:
        return url.lower()

    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "").rstrip("/")

    kept = []
    for k, v in parse_qsl(p.query):
        kl = k.lower()
        if kl in _KEEP_PARAMS and v:
            kept.append((kl, v))
    kept.sort()
    query = "&".join(f"{k}={v}" for k, v in kept)

    key = f"{host}{path}"
    if query:
        key += f"?{query}"
    return key or url.lower()


def all_url_keys(job: dict) -> list[str]:
    """一个职位（含多来源与同源备用 URL）的全部 url_key。"""
    urls = [job.get("url", "")]
    for rs in job.get("raw_sources") or []:
        if isinstance(rs, dict) and rs.get("url"):
            urls.append(rs["url"])
    for alt in job.get("alt_urls") or []:
        if isinstance(alt, str) and alt:
            urls.append(alt)
    keys = []
    for u in urls:
        k = canonicalize_url(u)
        if k and k not in keys:
            keys.append(k)
    return keys
