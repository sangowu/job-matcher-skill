"""A round searches and filters in the language of the market it targets."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import candidate_contract  # noqa: E402
import job_prefilter  # noqa: E402
import market_plan  # noqa: E402
import render_html  # noqa: E402
import source_registry  # noqa: E402
from _jobutil import internal_languages, language_aliases  # noqa: E402


@pytest.fixture(scope="module")
def resources():
    return market_plan.load_resources()


def _plan(resources, location, roles, **intent):
    markets, taxonomy = resources
    return market_plan.build_market_plan(
        {
            "cv_profile": {"preferred_roles": roles, "skills": ["Python"]},
            "user_intent": {"locations": [location], **intent},
        },
        markets=markets,
        taxonomy=taxonomy,
        max_websearch_calls=6,
        multi_region_enabled=False,
    )


# --- one language catalog ---------------------------------------------------

def test_every_module_reads_the_same_language_set():
    catalog = set(internal_languages())

    assert {"en", "de", "zh-Hans", "fr", "es", "pt", "ja", "ko", "it", "nl", "pl"} <= catalog
    assert set(market_plan.INTERNAL_LANGUAGES) == catalog
    assert set(source_registry.INTERNAL_LANGUAGES) == catalog
    assert set(candidate_contract.INTERNAL_LANGUAGES) == catalog
    assert set(render_html.INTERNAL_LANGUAGES) == catalog


def test_no_module_writes_the_language_set_out_by_hand():
    """The triple lived in four modules; a fourth language meant four edits."""
    pattern = re.compile(r'[({]\s*"en",\s*"de",\s*"zh-Hans"\s*[)}]')
    offenders = [
        f"{path.name}: {line.strip()}"
        for directory in ("scripts", "tools")
        for path in (SKILL_ROOT / directory).glob("*.py")
        for line in path.read_text(encoding="utf-8").splitlines()
        # The languages a market must be *named* in is a different, deliberate
        # set: names in the newer languages are optional.
        if pattern.search(line) and not line.startswith("LABEL_LANGUAGES")
    ]
    assert not offenders


@pytest.mark.parametrize(
    ("alias", "code"),
    [("Français", "fr"), ("pt-BR", "pt"), ("español", "es"), ("日本語", "ja"),
     ("Deutsch", "de"), ("简体中文", "zh-Hans"), ("English", "en"), ("한국어", "ko"),
     ("ko-KR", "ko"), ("Italiano", "it"), ("nl-BE", "nl"), ("Nederlands", "nl"), ("polski", "pl")],
)
def test_a_language_is_recognized_however_a_person_writes_it(alias, code):
    assert language_aliases()[alias.casefold()] == code
    assert market_plan.normalize_language(alias) == code


def test_every_role_family_is_titled_in_every_language(resources):
    _, taxonomy = resources
    for family in taxonomy["role_families"]:
        for group in ("titles", "synonyms"):
            assert set(family[group]) == set(internal_languages()), family["role_family_id"]


# --- the filter keeps a posting in the market's language ---------------------

@pytest.mark.parametrize(
    "title",
    [
        "Ingénieur Logiciel H/F",
        "Developpeur logiciel (x/f/m)",
        "Desarrollador de Software Senior",
        "Ingeniero de Software",
        "Engenheiro de Software Sênior",
        "ソフトウェアエンジニア（バックエンド）",
        "Softwareentwickler (m/w/d)",
        "Software-Entwickler Java",
        "Sviluppatore Software",
        "Ingegnere del Software Senior",
        "Softwareontwikkelaar",
        "Inżynier Oprogramowania",
        "Programista Java",
        "소프트웨어 엔지니어",
        "소프트웨어 개발자 (경력)",
    ],
)
def test_a_software_title_in_the_markets_language_is_kept(title):
    profile = {"roles": ["Software Engineer"], "skills": ["Python"]}

    assert job_prefilter.rejection_reason({"title": title, "location": ""}, profile) is None


@pytest.mark.parametrize(
    ("title", "role"),
    [
        ("Développeur Backend Python - CDI", "Backend Engineer"),
        ("Desenvolvedor Backend Pleno", "Backend Engineer"),
        ("バックエンドエンジニア", "Backend Engineer"),
        ("Ingénieur Data (H/F)", "Data Engineer"),
        ("Ingeniero de Datos", "Data Engineer"),
        ("Engenheiro de Dados", "Data Engineer"),
        ("データエンジニア", "Data Engineer"),
        ("Ingénieur IA générative", "AI Engineer"),
        ("機械学習エンジニア", "Machine Learning Engineer"),
        ("Desarrollador Frontend React", "Frontend Engineer"),
        ("フルスタックエンジニア", "Full Stack Engineer"),
        ("Sviluppatore Backend Java", "Backend Engineer"),
        ("Backend-ontwikkelaar", "Backend Engineer"),
        ("Programista Backend (Python)", "Backend Engineer"),
        ("백엔드 개발자", "Backend Engineer"),
        ("Inżynier Danych", "Data Engineer"),
        ("데이터 엔지니어", "Data Engineer"),
        ("Ingegnere AI", "AI Engineer"),
        ("Specjalista ds. sztucznej inteligencji", "AI Engineer"),
        ("머신러닝 엔지니어", "Machine Learning Engineer"),
        ("Programista Frontend React", "Frontend Engineer"),
        ("프론트엔드 개발자", "Frontend Engineer"),
        ("Webontwikkelaar", "Frontend Engineer"),
        ("풀스택 개발자", "Full Stack Engineer"),
    ],
)
def test_other_families_are_recognized_in_the_markets_language(title, role):
    profile = {"roles": [role]}

    assert job_prefilter.rejection_reason({"title": title, "location": ""}, profile) is None


@pytest.mark.parametrize(
    ("title", "role"),
    [
        ("Sviluppatore/Sviluppatrice Software", "Software Engineer"),
        ("Sviluppatore/Sviluppatrice Java", "Backend Engineer"),
        ("Ingegnere software Java", "Software Engineer"),
        ("Ingeniero/a de IA", "AI Engineer"),
        ("Ingeniero Software", "Software Engineer"),
        ("Desarrollador/a Python", "Python Backend Engineer"),
        ("Engenheiro(a) de Software", "Software Engineer"),
        ("Desenvolvedor(a) Python", "Python Backend Engineer"),
        ("Développeur·euse logiciel", "Software Engineer"),
        ("Développeur Python H/F", "Python Backend Engineer"),
        ("Softwareentwickler:in", "Software Engineer"),
        ("Python-Entwickler (m/w/d)", "Python Backend Engineer"),
        ("Python-ontwikkelaar", "Python Backend Engineer"),
        ("Programista/Programistka Python", "Python Backend Engineer"),
        ("Pythonエンジニア", "Python Backend Engineer"),
        ("Python 개발자", "Python Backend Engineer"),
    ],
)
def test_a_title_written_in_both_genders_or_without_its_preposition_is_kept(title, role):
    """2026-10-14: a Milan round dropped "Sviluppatore/Sviluppatrice Software"
    and "Ingegnere software Java" on role -- the vocabulary lists one gender
    and "Ingegnere del software" -- and only English, Italian and Polish named
    a developer by the language they write."""
    profile = {"roles": [role]}

    assert job_prefilter.rejection_reason({"title": title, "location": ""}, profile) is None


@pytest.mark.parametrize(
    "title",
    ["Ingegnere/geometra", "Ingegnere strutturista", "Ingegnere di Processo",
     "Ingeniero/a de Caminos", "Sviluppatore/a commerciale"],
)
def test_reading_a_title_both_ways_does_not_widen_it_to_other_engineers(title):
    profile = {"roles": ["AI Engineer", "Software Engineer"]}

    assert job_prefilter.rejection_reason({"title": title, "location": ""}, profile) == "role"


@pytest.mark.parametrize(
    "title",
    ["Commercial(e) sédentaire (x/f/m)", "Asesor/a comercial autónomo/a", "営業マネージャー",
     "Addetto alle vendite", "Accountmanager", "Przedstawiciel handlowy", "영업 관리자"],
)
def test_an_unrelated_title_in_another_language_is_still_refused(title):
    profile = {"roles": ["Software Engineer"]}

    assert job_prefilter.rejection_reason({"title": title, "location": ""}, profile) == "role"


@pytest.mark.parametrize(
    ("title", "level"),
    [
        ("Stage - Développeur logiciel", "intern"),
        ("Stagiaire Data Engineer", "intern"),
        ("Becario de Desarrollo", "intern"),
        ("Estágio em Engenharia de Software", "intern"),
        ("Werkstudent Softwareentwicklung", "intern"),
        ("ソフトウェアエンジニア インターン", "intern"),
        ("Jeune diplômé - Ingénieur Logiciel", "new_grad"),
        ("新卒 エンジニア", "new_grad"),
        ("Desenvolvedor Júnior", "junior"),
        ("Ingénieur Logiciel Confirmé", "mid"),
        ("Desenvolvedor Pleno", "mid"),
        ("Ingénieur Logiciel Sénior", "senior"),
        ("Engenheiro de Software Sênior", "senior"),
        ("シニアエンジニア", "senior"),
        ("Responsable Développement", "lead"),
        ("Jefe de Ingeniería", "lead"),
        ("Gerente de Engenharia", "lead"),
        ("Teamleiter Softwareentwicklung", "lead"),
        ("Director of Engineering", "lead"),
        ("テックリード", "lead"),
        ("Stagista Sviluppatore Software", "intern"),
        ("Tirocinio - Ingegnere del software", "intern"),
        ("Afstudeerstage Software Engineering", "intern"),
        ("Stażysta - Programista Java", "intern"),
        ("백엔드 개발 인턴", "intern"),
        ("Neolaureato Ingegneria Informatica", "new_grad"),
        ("신입 백엔드 개발자", "new_grad"),
        ("Młodszy Programista Java", "junior"),
        ("Regular Java Developer", "mid"),
        ("Medior Software Engineer", "mid"),
        ("Starszy Programista", "senior"),
        ("시니어 백엔드 엔지니어", "senior"),
        ("Responsabile Sviluppo Software", "lead"),
        ("Teamleider Development", "lead"),
        ("Kierownik Zespołu IT", "lead"),
        ("개발 팀장", "lead"),
    ],
)
def test_seniority_is_read_in_the_markets_language(title, level):
    assert level in job_prefilter._detected_levels(title)


@pytest.mark.parametrize(
    "title",
    ["Backstage Technician", "Confirmed Bookings Analyst", "Software Engineer"],
)
def test_a_level_word_inside_another_word_is_not_a_level(title):
    assert job_prefilter._detected_levels(title) == set()


def test_folding_takes_latin_accents_and_leaves_kana_alone():
    assert job_prefilter._fold("Développeur Sênior") == "Developpeur Senior"
    assert job_prefilter._fold("データ") == "データ"
    assert job_prefilter._fold("ＡＩエンジニア") == "AIエンジニア"


# --- the plan searches in the market's language -----------------------------

@pytest.mark.parametrize(
    ("location", "market", "language", "first_query"),
    [
        ("Paris", "fr", "fr", "offre d'emploi Ingénieur Logiciel Paris"),
        ("東京", "jp", "ja", "ソフトウェアエンジニア 求人 東京"),
        ("Madrid", "es", "es", "ofertas de empleo Ingeniero de Software Madrid"),
        ("São Paulo", "br", "pt", "vagas Engenheiro de Software São Paulo"),
        ("Wien", "at", "de", "Softwareentwickler Stellenangebote Wien"),
        ("Dublin", "ie", "en", "Software Engineer jobs Dublin"),
        ("서울", "kr", "ko", "서울 소프트웨어 엔지니어 채용"),
        ("Milano", "it", "it", "offerte di lavoro Ingegnere del software Milano"),
        ("Warszawa", "pl", "pl", "praca Inżynier oprogramowania Warszawa"),
    ],
)
def test_the_first_query_is_in_the_markets_own_language(
    resources, location, market, language, first_query
):
    plan = _plan(resources, location, ["Software Engineer"])

    assert plan["target_markets"] == [market]
    assert plan["search_languages_by_market"][market][0] == language
    assert plan["search_plan"][0]["query_string"] == first_query
    assert plan["roles_needing_translation"] == []


def test_a_bilingual_market_also_searches_in_english(resources):
    plan = _plan(resources, "Paris", ["Software Engineer"])

    assert {row["language"] for row in plan["search_plan"]} == {"fr", "en"}


@pytest.mark.parametrize(
    ("location", "languages"),
    [("Amsterdam", ["en", "nl"]), ("Antwerpen", ["fr", "nl", "en"]), ("Kraków", ["pl", "en"])],
)
def test_a_market_searches_every_language_its_postings_are_written_in(
    resources, location, languages
):
    plan = _plan(resources, location, ["Software Engineer"])
    market = plan["target_markets"][0]

    assert plan["search_languages_by_market"][market] == languages
    assert {row["language"] for row in plan["search_plan"]} == set(languages)


# --- roles the taxonomy does not know ----------------------------------------

def test_a_role_outside_the_taxonomy_is_reported_for_translation(resources):
    plan = _plan(resources, "Paris", ["Account Executive"])

    assert plan["roles_needing_translation"] == [
        {"role": "Account Executive", "languages": ["fr"]}
    ]
    assert any("localized_roles" in warning for warning in plan["warnings"])
    # Still a plan: the role is searched in its own spelling meanwhile.
    assert plan["needs_user_input"] is False


def test_supplied_translations_are_searched_and_kept_by_the_filter(resources):
    request = {
        "cv_profile": {"preferred_roles": ["Account Executive"]},
        "user_intent": {
            "locations": ["Paris"],
            "localized_roles": {"Account Executive": {"fr": ["Commercial", "Chargé de comptes"]}},
        },
    }
    markets, taxonomy = resources
    plan = market_plan.build_market_plan(
        request, markets=markets, taxonomy=taxonomy, max_websearch_calls=6,
        multi_region_enabled=False,
    )
    profile = market_plan.build_effective_profile(request, markets=markets, taxonomy=taxonomy)

    queries = [row["query_string"] for row in plan["search_plan"]]
    assert "offre d'emploi Commercial Paris" in queries
    assert "offre d'emploi Chargé de comptes Paris" in queries
    assert plan["roles_needing_translation"] == []
    assert profile["roles"] == ["Account Executive", "Commercial", "Chargé de comptes"]
    kept = job_prefilter.rejection_reason(
        {"title": "Commercial(e) sédentaire (x/f/m)", "location": "Paris"}, profile
    )
    assert kept is None


def test_a_role_written_in_the_markets_language_needs_no_translation_there(resources):
    plan = _plan(resources, "Paris", ["Comptable"], role_language="fr")

    assert plan["role_language"] == "fr"
    assert plan["roles_needing_translation"] == [{"role": "Comptable", "languages": ["en"]}]


@pytest.mark.parametrize(
    ("localized", "message"),
    [
        ({"Someone Else": {"fr": ["X"]}}, "not searching"),
        ({"Account Executive": {"sv": ["X"]}}, "unsupported language"),
        ({"Account Executive": {"fr": ["a", "b", "c", "d"]}}, "1-3 titles"),
        ({"Account Executive": {"fr": []}}, "1-3 titles"),
        ({"Account Executive": "Commercial"}, "map languages"),
        ("Commercial", "must be an object"),
    ],
)
def test_malformed_translations_are_refused(resources, localized, message):
    with pytest.raises(market_plan.MarketPlanError, match=message):
        _plan(resources, "Paris", ["Account Executive"], localized_roles=localized)


def test_the_language_catalog_is_listed_in_both_readmes():
    for name in ("README.md", "README.en.md"):
        assert "languages.json" in (SKILL_ROOT / name).read_text(encoding="utf-8")


def test_the_catalog_file_is_valid_and_ordered_with_english_first():
    payload = json.loads((SKILL_ROOT / "references" / "languages.json").read_text(encoding="utf-8"))

    assert payload["languages"][0]["code"] == "en"
    for language in payload["languages"]:
        assert "{role}" in language["query_template"]
        assert "{location}" in language["query_template"]
