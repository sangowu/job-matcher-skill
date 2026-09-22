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
            "visible": True,
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
        "visible": True,
    }

    result = classify_cookie_consent(value)

    assert result["decision"] == "auto_select_necessary_only"
    assert result["target_ref"] == "e2"


def test_ask_policy_non_cookie_dialog_and_ambiguous_actions_pause():
    assert classify_cookie_consent(payload("Just Necessary", policy="ask_every_time"))[
        "reason"
    ] == "policy_requires_user"

    value = payload("Just Necessary")
    value["container"] = {
        "role": "dialog",
        "name": "Sign in",
        "text": "Login",
        "visible": True,
    }
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


def _hidden_shell(**overrides) -> dict:
    """The shape observed live on a public-sector site on 2026-09-22: consent had
    already been given, and an empty, invisible role=dialog shell remained in the
    DOM under a name that reads like a button."""
    container = {
        "role": "dialog",
        "name": "Cookie consent button",
        "text": "",
        "visible": False,
    }
    container.update(overrides)
    return {"policy": "necessary_only", "container": container, "controls": []}


def test_an_invisible_consent_shell_does_not_pause_the_run():
    result = classify_cookie_consent(_hidden_shell())

    assert result["decision"] == "proceed"
    assert result["reason"] == "consent_dialog_not_displayed"
    assert result["target_ref"] is None


def test_the_same_shell_while_displayed_still_pauses():
    """Visibility must not become a way to act on a banner that is showing."""
    result = classify_cookie_consent(_hidden_shell(visible=True))

    assert result["decision"] == "pause"
    assert result["target_ref"] is None


def test_an_invisible_dialog_is_never_acted_on_even_with_a_safe_button():
    payload = _hidden_shell()
    payload["controls"] = [{"ref": "e1", "role": "button", "name": "Only necessary"}]

    result = classify_cookie_consent(payload)

    assert result["decision"] == "proceed"
    assert result["target_ref"] is None


def test_ask_every_time_still_wins_over_visibility():
    payload = _hidden_shell()
    payload["policy"] = "ask_every_time"

    result = classify_cookie_consent(payload)

    assert result["decision"] == "pause"
    assert result["reason"] == "policy_requires_user"


@pytest.mark.parametrize("value", ["true", 1, None, "False"])
def test_non_boolean_visibility_is_refused(value):
    payload = _hidden_shell()
    payload["container"]["visible"] = value

    with pytest.raises(CookieConsentError, match="visible"):
        classify_cookie_consent(payload)


def test_container_without_visibility_is_refused():
    payload = _hidden_shell()
    del payload["container"]["visible"]

    with pytest.raises(CookieConsentError, match="visible"):
        classify_cookie_consent(payload)
