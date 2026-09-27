#!/usr/bin/env python3
"""The deterministic prefilter every discovery channel is held to.

It lived in `ats_pipeline.py`, so only the structured channel was held to it.
The browser and Web Search channels reported `candidates_prefiltered` as a
number a worker had arrived at by its own reading, and `discovery_batch.py`
checked only that the funnel did not widen -- so the same round ran two
rules. The visible cost was remote postings: the structured channel skips
any location naming remote, and a "Remote - Ireland" posting reached the
table through the browser anyway. The rule is about jobs, not about ATS, and
this module is where it lives now; `ats_pipeline` imports it and its own
callers are unchanged.
"""
from __future__ import annotations

import re
from typing import Any


_REMOTE_TERMS = ("remote", "anywhere", "distributed", "远程")
_LEVEL_TERMS = {
    "intern": ("intern", "internship", "实习"),
    "new_grad": ("graduate", "new grad", "entry level", "校招", "应届"),
    "junior": ("junior", "jr ", "初级"),
    "mid": ("mid-level", "mid level", "intermediate", "中级"),
    "senior": ("senior", "sr ", "资深", "高级"),
    "lead": ("lead", "principal", "staff", "architect", "manager", "主管", "负责人"),
}
_ROLE_FAMILIES = {
    "applied_ai": (
        "ai engineer", "ai developer", "artificial intelligence", "machine learning",
        "ml engineer", "ai evaluation", "ai systems", "agent systems", "agentic ai",
        "llm", "nlp", "data scientist", "算法工程师", "人工智能", "机器学习",
    ),
    "backend": ("backend", "back-end", "server-side", "后端"),
    "frontend": ("frontend", "front-end", "前端"),
    "fullstack": ("full stack", "full-stack", "全栈"),
    "data": ("data engineer", "analytics engineer", "数据工程"),
    "platform": ("devops", "site reliability", "sre", "cloud engineer", "平台工程"),
    "product": ("product manager", "产品经理"),
}
# A family term above is a phrase, matched as a substring, so one word
# between two of its words hides it: "AI Platform Engineer" carries neither
# "ai engineer" nor "machine learning", and on 2026-09-27 the filter dropped
# it, "AI Infrastructure Engineer" and "ML Platform Engineer" for a profile
# whose every preferred role is an AI role. These are matched as whole tokens
# instead, which is why they cannot live in the table above: "ai" as a
# substring is inside "maintenance" and "training".
_ROLE_FAMILY_TOKENS = {
    "applied_ai": frozenset({"ai", "ml", "aiml", "llm", "genai", "nlp"}),
}
# Where such a token sits is the whole difference between a role and a
# product. "AI Platform Engineer" qualifies the engineer; "Mobile Application
# Developer - AI Neobank App" qualifies the app, and that one must stay out.
_TITLE_SEGMENT = re.compile(r"[,\-–—(){}\[\]|:;]+")

_GENERIC_TITLE_TOKENS = {
    "ai", "artificial", "intelligence", "engineer", "engineering", "developer",
    "specialist", "manager", "lead", "senior", "junior", "staff", "principal",
    "工程师", "开发", "经理", "高级", "初级",
}

def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w+#.]+", " ", value or "").lower()).strip()


def _title_segments(value: str) -> list[str]:
    parts = (_normalized_text(part) for part in _TITLE_SEGMENT.split(value or ""))
    return [part for part in parts if part]


def _family_tokens_match(value: str, family_tokens: frozenset[str]) -> bool:
    segments = _title_segments(value)
    if not segments:
        return False
    # The first segment is the role phrase itself.
    if set(segments[0].split()) & family_tokens:
        return True
    # A later segment that is nothing but the qualifier names no product, so
    # "Software Engineer, AI" is still an AI role.
    return any(
        (tokens := set(segment.split())) and tokens <= family_tokens
        for segment in segments[1:]
    )


def _role_families(value: str) -> set[str]:
    normalized = _normalized_text(value)
    families = {
        family for family, terms in _ROLE_FAMILIES.items()
        if any(_normalized_text(term) in normalized for term in terms)
    }
    families.update(
        family for family, tokens in _ROLE_FAMILY_TOKENS.items()
        if _family_tokens_match(value, tokens)
    )
    return families


def _title_matches(title: str, roles: list[str]) -> bool:
    if not roles:
        return True
    normalized_title = _normalized_text(title)
    title_families = _role_families(title)
    title_tokens = set(normalized_title.split()) - _GENERIC_TITLE_TOKENS
    for role in roles:
        normalized_role = _normalized_text(str(role))
        if normalized_role and (
            normalized_role in normalized_title or normalized_title in normalized_role
        ):
            return True
        if title_families & _role_families(str(role)):
            return True
        role_tokens = set(normalized_role.split()) - _GENERIC_TITLE_TOKENS
        if title_tokens & role_tokens:
            return True
    return False


def _location_matches(location: str, locations: list[str]) -> bool:
    normalized = _normalized_text(location)
    if not normalized:
        return True
    # Ahead of the "no preferred locations" shortcut, not behind it. A profile
    # that names no locations is the case this skill is written for --
    # `extract_cv.py` reports `target_locations` as missing for it, which is
    # the same gap that let a market-scoped round fetch the whole world --
    # and the shortcut returned True before the remote check ever ran. So the
    # rule in WORKFLOW step 3, that a location naming remote is skipped
    # whatever else it names, was dead for exactly the profile it was written
    # for: two remote postings sit in the table on 2026-09-26, both through
    # the structured channel that is supposed to skip them.
    if any(term in normalized for term in _REMOTE_TERMS):
        return False
    if not locations:
        return True
    return any(
        (preferred := _normalized_text(str(value)))
        and (preferred in normalized or normalized in preferred)
        for value in locations
    )


def _seniority_matches(title: str, blocked_levels: list[str]) -> bool:
    normalized = f"{_normalized_text(title)} "
    detected = {
        level for level, terms in _LEVEL_TERMS.items()
        if any(term in normalized for term in terms)
    }
    return not (detected & {str(level) for level in blocked_levels})


def prefilter_jobs(jobs: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    roles = [str(value) for value in (profile.get("roles") or profile.get("preferred_roles") or [])]
    locations = [
        str(value) for value in (
            profile.get("locations") or profile.get("preferred_locations") or []
        )
    ]
    blocked_levels = [str(value) for value in (profile.get("blocked_levels") or [])]
    return [
        job for job in jobs
        if _title_matches(str(job.get("title") or ""), roles)
        and _location_matches(str(job.get("location") or ""), locations)
        and _seniority_matches(str(job.get("title") or ""), blocked_levels)
    ]


REJECTION_REASONS = ("role", "location", "seniority")


def rejection_reason(job: dict[str, Any], profile: dict[str, Any]) -> str | None:
    """Why the prefilter refuses this posting, or None if it keeps it.

    Same three checks as `prefilter_jobs`, reported one at a time so a caller
    can count what it dropped and why. The values are a closed set, because
    the count goes into metrics.
    """
    roles = [str(value) for value in (profile.get("roles") or profile.get("preferred_roles") or [])]
    locations = [
        str(value) for value in (
            profile.get("locations") or profile.get("preferred_locations") or []
        )
    ]
    blocked_levels = [str(value) for value in (profile.get("blocked_levels") or [])]
    title = str(job.get("title") or "")
    if not _title_matches(title, roles):
        return "role"
    if not _location_matches(str(job.get("location") or ""), locations):
        return "location"
    if not _seniority_matches(title, blocked_levels):
        return "seniority"
    return None
