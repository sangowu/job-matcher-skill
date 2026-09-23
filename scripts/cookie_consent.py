#!/usr/bin/env python3
"""Classify a bounded accessibility-tree cookie-consent action.

The classifier never reads browser cookies and never clicks anything. It only
accepts a consent-container summary plus semantic controls from the current
snapshot, then returns either one unambiguous necessary-only target or a pause.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any
from _stdio import StdinUnavailable, read_stdin_text


POLICIES = {"necessary_only", "ask_every_time"}
CONTAINER_ROLES = {"dialog", "alertdialog"}
CONTROL_ROLES = {"button", "link", "checkbox", "switch"}
SAFE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}")
COOKIE_SIGNALS = ("cookie", "cookies", "tracking", "cookie einstellungen")
SAFE_NECESSARY_ONLY_LABELS = {
    # English
    "just necessary",
    "necessary only",
    "accept necessary only",
    "accept strictly necessary",
    "use necessary cookies only",
    "reject all",
    "reject cookies",
    "reject optional cookies",
    "reject non essential cookies",
    "decline optional cookies",
    "continue without accepting",
    # German
    "nur notwendige cookies",
    "nur notwendige cookies akzeptieren",
    "nur erforderliche cookies",
    "alle ablehnen",
    "optionale cookies ablehnen",
    "mit notwendigen cookies fortfahren",
    # Simplified Chinese
    "仅必要 cookie",
    "仅允许必要 cookie",
    "只允许必要 cookie",
    "仅使用必要 cookie",
    "拒绝非必要 cookie",
    "拒绝可选 cookie",
    "全部拒绝",
    # Traditional Chinese
    "僅必要 cookie",
    "僅允許必要 cookie",
    "只允許必要 cookie",
    "僅使用必要 cookie",
    "拒絕非必要 cookie",
    "拒絕可選 cookie",
    "全部拒絕",
    "拒絕全部",
}


class CookieConsentError(ValueError):
    """Raised when classifier input violates the public contract."""


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE).split())


def _bounded_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise CookieConsentError(f"{field} must be a string of at most {limit} characters")
    return value


def classify_cookie_consent(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "policy",
        "container",
        "controls",
    }:
        raise CookieConsentError("input must contain policy, container, and controls")
    policy = payload["policy"]
    if policy not in POLICIES:
        raise CookieConsentError("unsupported cookie consent policy")
    container = payload["container"]
    if not isinstance(container, dict) or set(container) != {
        "role",
        "name",
        "text",
        "visible",
    }:
        raise CookieConsentError(
            "container must contain role, name, text, and visible"
        )
    if not isinstance(container["visible"], bool):
        raise CookieConsentError("container.visible must be boolean")
    role = _bounded_text(container["role"], "container.role", 40).casefold()
    name = _bounded_text(container["name"], "container.name", 160)
    text = _bounded_text(container["text"], "container.text", 500)
    controls = payload["controls"]
    if not isinstance(controls, list) or len(controls) > 30:
        raise CookieConsentError("controls must be a list with at most 30 items")

    normalized_controls: list[dict[str, str]] = []
    for index, control in enumerate(controls):
        if not isinstance(control, dict) or set(control) != {"ref", "role", "name"}:
            raise CookieConsentError(f"controls[{index}] is invalid")
        ref = _bounded_text(control["ref"], f"controls[{index}].ref", 80)
        control_role = _bounded_text(
            control["role"], f"controls[{index}].role", 40
        ).casefold()
        control_name = _bounded_text(
            control["name"], f"controls[{index}].name", 160
        )
        if not SAFE_REF.fullmatch(ref) or control_role not in CONTROL_ROLES:
            raise CookieConsentError(f"controls[{index}] has an invalid ref or role")
        normalized_controls.append(
            {"ref": ref, "role": control_role, "name": _normalize(control_name)}
        )

    if policy == "ask_every_time":
        return {
            "decision": "pause",
            "reason": "policy_requires_user",
            "target_ref": None,
        }
    # A consent dialog that is not showing blocks nothing. Sites commonly leave
    # an empty dialog shell in the DOM after consent was already given, and
    # classifying that shell as ambiguous paused runs over a banner no human
    # could see. This never selects a control; it only declines to stop.
    if not container["visible"]:
        return {
            "decision": "proceed",
            "reason": "consent_dialog_not_displayed",
            "target_ref": None,
        }
    context = _normalize(f"{name} {text}")
    if role not in CONTAINER_ROLES or not any(
        signal in context for signal in COOKIE_SIGNALS
    ):
        return {
            "decision": "pause",
            "reason": "not_cookie_consent_dialog",
            "target_ref": None,
        }
    matches = [
        control
        for control in normalized_controls
        if control["role"] == "button"
        and control["name"] in SAFE_NECESSARY_ONLY_LABELS
    ]
    if len(matches) != 1:
        return {
            "decision": "pause",
            "reason": (
                "no_safe_semantic_action"
                if not matches
                else "ambiguous_safe_semantic_actions"
            ),
            "target_ref": None,
        }
    return {
        "decision": "auto_select_necessary_only",
        "reason": "unique_safe_semantic_action",
        "target_ref": matches[0]["ref"],
    }


def main() -> int:
    try:
        payload = json.loads(read_stdin_text())
        result = classify_cookie_consent(payload)
    except (CookieConsentError, StdinUnavailable, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
