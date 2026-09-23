#!/usr/bin/env python3
"""模块2：CVProfile 校验 + 确定性补全。

读 LLM 抽取的 CVProfile JSON（stdin），做：
1. 剥离 markdown 包裹、解析 JSON
2. 字段类型/枚举校验、容错填默认值
3. 由 seniority 单值映射出 eligible/stretch/blocked levels（借鉴 JobRadar seniority.py）
4. 语言 code 归一、roles/skills 去重、加 schema_version

零依赖（仅标准库），保证 skill 可移植、与 JobRadar 解耦。

用法:  python validate_profile.py        (从 stdin 读 JSON)
输出:  {ok, profile, notes} | {ok: false, error}
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from _stdio import StdinUnavailable, read_stdin_text

SCHEMA_VERSION = "1.0"
SKILL_ROOT = Path(__file__).resolve().parent.parent

# ── seniority 映射（移植自 JobRadar jobradar/seniority.py）────────────────────────
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lead", ("vice president", "vp", "head of", "director", "distinguished", "fellow", "cto", "cio", "cso")),
    ("lead", ("principal", "architect", "staff", "manager", "engineering manager", "tech lead", "team lead")),
    ("senior", ("senior", "sr", "technical lead", "supervisor", "高级", "资深")),
    ("mid", ("mid", "mid-level", "mid level", "intermediate", "experienced", "ii", "iii", "中级")),
    ("junior", ("junior", "jr", "entry level", "entry-level", "entry", "associate", "assistant", "初级")),
    ("new_grad", ("new grad", "new graduate", "graduate program", "graduate programme", "graduate", "应届")),
    ("intern", ("intern", "internship", "placement", "apprentice", "trainee", "实习")),
)
_ALIASES = {
    "graduate": "new_grad", "entry": "junior", "entry_level": "junior",
    "associate": "junior", "staff": "lead", "principal": "lead",
    "manager": "lead", "director": "lead",
}
_LEVEL_RANK = {"intern": 0, "new_grad": 1, "junior": 2, "mid": 3, "senior": 4, "lead": 5}
_VALID_LEVELS = set(_LEVEL_RANK)


def _norm_text(value: str) -> str:
    return value.strip().lower()


def normalize_seniority_level(level: str) -> str:
    raw = _norm_text(level) if level else "unknown"
    raw = _ALIASES.get(raw, raw)
    return raw if raw in _VALID_LEVELS else "unknown"


def normalize_seniority(raw_values: list[str], fallback: str = "unknown") -> str:
    best_match, best_rank = "unknown", -1
    for raw_value in raw_values:
        text = _norm_text(str(raw_value))
        if not text:
            continue
        for level, keywords in _KEYWORDS:
            if any(k in text for k in keywords):
                rank = _LEVEL_RANK[level]
                if rank > best_rank:
                    best_match, best_rank = level, rank
    if best_match != "unknown":
        return best_match
    fb = normalize_seniority_level(fallback)
    if fb != "unknown":
        return fb
    fb_text = _norm_text(fallback)
    for level, keywords in _KEYWORDS:
        if any(k in fb_text for k in keywords):
            return level
    return "unknown"


def default_eligible_levels(level: str) -> list[str]:
    return {
        "intern": ["intern"],
        "new_grad": ["intern", "new_grad", "junior"],
        "junior": ["new_grad", "junior", "mid"],
        "mid": ["junior", "mid", "senior"],
        "senior": ["mid", "senior", "lead"],
        "lead": ["senior", "lead"],
        "unknown": ["new_grad", "junior", "mid"],
    }[level]


def default_stretch_levels(level: str, mode: str) -> list[str]:
    if mode == "strict":
        return []
    return {
        "intern": ["new_grad"] if mode == "stretch" else [],
        "new_grad": ["mid"] if mode == "stretch" else ["junior"],
        "junior": ["senior"] if mode == "stretch" else ["mid"],
        "mid": ["lead"] if mode == "stretch" else ["senior"],
        "senior": ["lead"],
        "lead": [],
        "unknown": ["senior"] if mode == "stretch" else ["mid"],
    }[level]


def default_blocked_levels(level: str) -> list[str]:
    return {
        "intern": ["junior", "mid", "senior", "lead"],
        "new_grad": ["senior", "lead"],
        "junior": ["lead"],
        "mid": ["lead"],
        "senior": [],
        "lead": [],
        "unknown": ["lead"],
    }[level]


# ── 语言 code 归一（精简自 JobRadar schemas._LANGUAGE_CODE_ALIASES）───────────────
_LANG_ALIASES = {
    "en": "en", "english": "en", "英语": "en", "英文": "en",
    "zh": "zh-Hans", "zh-cn": "zh-Hans", "zh_hans": "zh-Hans",
    "zh-hans": "zh-Hans", "chinese": "zh-Hans", "中文": "zh-Hans",
    "汉语": "zh-Hans", "mandarin": "zh-Hans", "普通话": "zh-Hans",
    "es": "es", "spanish": "es", "西班牙语": "es",
    "de": "de", "german": "de", "德语": "de",
    "fr": "fr", "french": "fr", "法语": "fr",
    "ja": "ja", "japanese": "ja", "日语": "ja", "日本語": "ja",
    "ko": "ko", "korean": "ko", "韩语": "ko",
    "pt": "pt", "portuguese": "pt", "葡萄牙语": "pt",
    "it": "it", "italian": "it", "意大利语": "it",
}


def normalize_lang_code(value: str) -> str:
    return _LANG_ALIASES.get(_norm_text(value), _norm_text(value))


# ── 工具 ────────────────────────────────────────────────────────────────────────
def strip_markdown_fence(text: str) -> str:
    """剥离 LLM 常加的 ```json ... ``` 代码块包裹，提取 JSON 主体。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    # 没有围栏：截取第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def dedupe_keep_order(items: list) -> list:
    seen, out = set(), []
    for it in items:
        if not isinstance(it, str):
            continue
        key = it.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(it.strip())
    return out


def load_seniority_mode() -> str:
    try:
        cfg = json.loads((SKILL_ROOT / "config.json").read_text(encoding="utf-8"))
        mode = cfg.get("seniority_mode", "balanced")
        return mode if mode in {"strict", "balanced", "stretch"} else "balanced"
    except Exception:
        return "balanced"


def _as_str(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _fail(error: str) -> None:
    print(json.dumps({"ok": False, "error": error}))
    sys.exit(1)


def main() -> None:
    try:
        raw = read_stdin_text()
    except StdinUnavailable as error:
        _fail(str(error))
    if not raw.strip():
        _fail("stdin 为空，未收到 CVProfile JSON")

    try:
        data = json.loads(strip_markdown_fence(raw))
    except json.JSONDecodeError as e:
        _fail(f"JSON 解析失败：{e}")
        return
    if not isinstance(data, dict):
        _fail("CVProfile 必须是 JSON 对象")
        return

    notes: list[str] = []
    mode = load_seniority_mode()

    # seniority 归一（容错：LLM 给 'Senior Engineer' 也能识别）
    raw_sen = _as_str(data.get("seniority"))
    seniority = normalize_seniority([raw_sen], raw_sen) if raw_sen else "unknown"
    if raw_sen and normalize_seniority_level(raw_sen) != seniority and seniority != "unknown":
        notes.append(f"seniority '{raw_sen}' 归一为 '{seniority}'")
    if seniority == "unknown":
        notes.append("seniority 未能判定，levels 用宽松默认")

    # years_of_experience
    yoe = data.get("years_of_experience")
    if isinstance(yoe, bool):
        yoe = None
    elif isinstance(yoe, (int, float)):
        yoe = float(yoe) if yoe >= 0 else None
    else:
        yoe = None

    # languages 归一
    languages = []
    for lang in data.get("languages") or []:
        if isinstance(lang, dict):
            name = _as_str(lang.get("name"))
            code = lang.get("code") or name
            languages.append({"name": name, "code": normalize_lang_code(str(code)), "level": _as_str(lang.get("level"))})
        elif isinstance(lang, str):
            languages.append({"name": lang.strip(), "code": normalize_lang_code(lang), "level": ""})

    preferred_roles = dedupe_keep_order(data.get("preferred_roles") or [])
    skills = dedupe_keep_order(data.get("skills") or [])

    missing = list(data.get("missing") or [])
    if not preferred_roles and "preferred_roles" not in missing:
        missing.append("preferred_roles")
        notes.append("preferred_roles 缺失，将依赖 query 补全")

    cv_language = normalize_lang_code(_as_str(
        data.get("cv_language") or data.get("search_language")
    ) or "en")
    report_language = normalize_lang_code(
        _as_str(data.get("report_language")) or cv_language
    )
    profile = {
        "schema_version": SCHEMA_VERSION,
        "summary": _as_str(data.get("summary")),
        "preferred_roles": preferred_roles,
        "skills": skills,
        "years_of_experience": yoe,
        "seniority": seniority,
        "eligible_levels": default_eligible_levels(seniority),
        "stretch_levels": default_stretch_levels(seniority, mode),
        "blocked_levels": default_blocked_levels(seniority),
        "preferred_locations": dedupe_keep_order(data.get("preferred_locations") or []),
        "target_locations": dedupe_keep_order(data.get("target_locations") or []),
        "current_location": _as_str(data.get("current_location")),
        "languages": languages,
        "industries": dedupe_keep_order(data.get("industries") or []),
        "education_level": _as_str(data.get("education_level")),
        "current_title": _as_str(data.get("current_title")),
        "cv_language": cv_language,
        "report_language": report_language,
        # Deprecated compatibility alias. New planning derives search languages
        # from the target market rather than this CV-language field.
        "search_language": cv_language,
        "missing": missing,
    }

    print(json.dumps({"ok": True, "profile": profile, "notes": notes}))


if __name__ == "__main__":
    main()
