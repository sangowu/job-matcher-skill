"""The report page speaks the language the round reports in."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import render_html  # noqa: E402
from _jobutil import internal_languages  # noqa: E402


def _tables() -> dict[str, dict[str, str]]:
    payload = json.loads(render_html.STRINGS_PATH.read_text(encoding="utf-8"))
    return payload["strings"]


def _placeholders(text: str) -> list[str]:
    return sorted(re.findall(r"\{\d\}", text))


@pytest.mark.parametrize("code", internal_languages())
def test_every_language_has_every_key_with_the_same_placeholders(code):
    tables = _tables()
    english = tables["en"]
    table = tables[code]

    assert set(table) == set(english)
    for key, text in english.items():
        assert _placeholders(table[key]) == _placeholders(text), (code, key)
        assert table[key].strip(), (code, key)


def test_no_table_is_kept_for_a_language_the_skill_does_not_search_in():
    assert set(_tables()) == set(internal_languages())


@pytest.mark.parametrize("code", [c for c in internal_languages() if c != "en"])
def test_a_language_table_is_a_translation_not_a_copy(code):
    """Copying the English table would pass the key check and show English."""
    english = _tables()["en"]
    table = _tables()[code]
    same = [key for key in english if table[key] == english[key]]

    # Codes and symbols stay as they are in every language: ATS, BrowserOS
    # Neo, "≥ {0}", "{0} ms", "Remote" in German.
    assert len(same) < len(english) // 5, same


def test_every_key_the_page_asks_for_exists():
    template = render_html.TEMPLATE_PATH.read_text(encoding="utf-8")
    english = _tables()["en"]
    # Whole keys only; `t("rec_" + rec)` builds its key at run time.
    asked = set(re.findall(r'\bt\("([a-z0-9_]+)"\s*[,)]', template))

    assert asked
    assert asked <= set(english), sorted(asked - set(english))


def test_the_page_carries_no_text_table_of_its_own():
    template = render_html.TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "const I18N" not in template
    assert "同一岗位的其他发布" not in template


@pytest.mark.parametrize(
    ("written", "code"),
    [
        ("fr", "fr"), ("Français", "fr"), ("pt-BR", "pt"), ("ja", "ja"),
        ("zh", "zh-Hans"), ("zh-CN", "zh-Hans"), ("zh-Hans", "zh-Hans"),
        ("de", "de"), ("es", "es"), ("en", "en"), ("ko-KR", "ko"), ("Italiano", "it"),
        ("nl-BE", "nl"), ("polski", "pl"),
        # Not a language the skill has text for: English, as before.
        ("sv", "en"), ("", "en"), (None, "en"),
    ],
)
def test_the_report_language_is_read_however_it_was_written(written, code):
    assert render_html.report_language(written) == code
    meta = render_html.normalize_report_meta({"report_language": written})
    assert meta["lang"] == meta["report_language"] == code


def test_an_old_meta_file_with_only_lang_still_works():
    assert render_html.normalize_report_meta({"lang": "zh"})["lang"] == "zh-Hans"


def test_a_missing_table_fails_when_the_report_is_rendered(tmp_path):
    payload = json.loads(render_html.STRINGS_PATH.read_text(encoding="utf-8"))
    del payload["strings"]["ja"]
    broken = tmp_path / "strings.json"
    broken.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(render_html.ReportStringsError, match="no table for ja"):
        render_html.report_strings(broken)


def test_a_missing_key_fails_when_the_report_is_rendered(tmp_path):
    payload = json.loads(render_html.STRINGS_PATH.read_text(encoding="utf-8"))
    del payload["strings"]["fr"]["apply"]
    broken = tmp_path / "strings.json"
    broken.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(render_html.ReportStringsError, match="fr lacks apply"):
        render_html.report_strings(broken)


def test_a_french_round_writes_french_text_and_language_list_into_its_data(
    monkeypatch, tmp_path, capsys
):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "jobs_table.json").write_text('{"jobs": []}', encoding="utf-8")
    meta_path = data_dir / "run_meta.json"
    meta_path.write_text(
        json.dumps({"report_language": "fr", "target_markets": ["fr"]}), encoding="utf-8"
    )
    monkeypatch.setattr(render_html, "DATA_DIR", data_dir)
    monkeypatch.setattr(render_html, "TABLE_PATH", data_dir / "jobs_table.json")
    monkeypatch.setattr(render_html, "REPORTS_DIR", data_dir / "reports")
    monkeypatch.setattr(render_html, "METRICS_PATH", data_dir / "metrics.jsonl")
    monkeypatch.setattr(render_html, "EVAL_RUNS_DIR", data_dir / "eval_runs")
    monkeypatch.setattr(render_html, "load_config", lambda: {})
    monkeypatch.setattr(
        sys,
        "argv",
        ["render_html.py", "--cv-hash", "a", "--cp-hash", "b",
         "--meta-file", str(meta_path), "--no-open"],
    )

    render_html.main()
    result = json.loads(capsys.readouterr().out)
    data = Path(result["report_data_path"]).read_text(encoding="utf-8")

    def constant(name: str):
        return json.loads(re.search(rf"^(?:const|let) {name} = (.*);$", data, re.M).group(1))

    assert constant("LANG") == "fr"
    assert constant("I18N")["fr"]["apply"] == "Postuler →"
    assert set(constant("I18N")) == set(internal_languages())
    languages = constant("LANGUAGES")
    assert [row["code"] for row in languages] == list(internal_languages())
    assert {"code": "fr", "name": "Français", "locale": "fr-FR"} in languages
    meta = constant("META")
    assert meta["market_names"]["fr"]["names"]["fr"] == "France"
