from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from cookie_consent import CookieConsentError, classify_cookie_consent  # noqa: E402


def payload(label: str, *, policy: str = "necessary_only") -> dict:
    return {
        "policy": policy,
        "container": {
            "role": "dialog",
            "name": "Cookie consent dialog",
            "text": "We use cookies for necessary functions and optional tracking.",
        },
        "controls": [
            {"ref": "e1", "role": "button", "name": "Accept All"},
            {"ref": "e2", "role": "button", "name": label},
            {"ref": "e3", "role": "button", "name": "Cookie Settings"},
        ],
    }


@pytest.mark.parametrize(
    "label",
    [
        "Just Necessary",
        "ACCEPT STRICTLY NECESSARY",
        "Reject Cookies",
        "Nur notwendige Cookies akzeptieren",
        "拒绝非必要 Cookie",
        "拒絕全部",
    ],
)
def test_unique_localized_necessary_only_action_is_selected(label):
    result = classify_cookie_consent(payload(label))

    assert result == {
        "decision": "auto_select_necessary_only",
        "reason": "unique_safe_semantic_action",
        "target_ref": "e2",
    }


def test_accept_all_is_never_selected():
    value = payload("Manage Preferences")

    result = classify_cookie_consent(value)

    assert result["decision"] == "pause"
    assert result["reason"] == "no_safe_semantic_action"
    assert result["target_ref"] is None


def test_traditional_chinese_consent_banner_uses_bounded_cookie_context():
    value = payload("拒絕全部")
    value["container"] = {
        "role": "dialog",
        "name": "consent banner",
        "text": "Privacy Statement Cookie Statement",
    }

    result = classify_cookie_consent(value)

    assert result["decision"] == "auto_select_necessary_only"
    assert result["target_ref"] == "e2"


def test_ask_policy_non_cookie_dialog_and_ambiguous_actions_pause():
    assert classify_cookie_consent(payload("Just Necessary", policy="ask_every_time"))[
        "reason"
    ] == "policy_requires_user"

    value = payload("Just Necessary")
    value["container"] = {"role": "dialog", "name": "Sign in", "text": "Login"}
    assert classify_cookie_consent(value)["reason"] == "not_cookie_consent_dialog"

    value = payload("Just Necessary")
    value["controls"].append(
        {"ref": "e4", "role": "button", "name": "Reject All"}
    )
    assert classify_cookie_consent(value)["reason"] == "ambiguous_safe_semantic_actions"


def test_classifier_rejects_unbounded_or_unknown_input():
    value = payload("Just Necessary")
    value["cookie_value"] = "secret"

    with pytest.raises(CookieConsentError, match="must contain"):
        classify_cookie_consent(value)


def test_cli_returns_only_decision_metadata():
    process = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "cookie_consent.py")],
        input=json.dumps(payload("Just Necessary")),
        text=True,
        capture_output=True,
        check=False,
    )

    assert process.returncode == 0
    result = json.loads(process.stdout)
    assert result["decision"] == "auto_select_necessary_only"
    assert result["target_ref"] == "e2"
    assert "Cookie consent dialog" not in process.stdout
