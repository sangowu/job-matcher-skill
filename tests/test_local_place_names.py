"""A place is recognized the way each language writes it."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import market_plan  # noqa: E402


@pytest.fixture(scope="module")
def markets():
    markets, _ = market_plan.load_resources()
    return markets


def _place(markets, text):
    placed = market_plan.normalize_location(text, markets)
    return placed["market_ids"][:1], placed["city_id"]


@pytest.mark.parametrize(
    ("text", "market", "city"),
    [
        # A generic market's city in five languages.
        ("Varsovie", "pl", "pl-warsaw"),
        ("Varsovia, Polonia", "pl", "pl-warsaw"),
        ("Varsóvia", "pl", "pl-warsaw"),
        ("Warschau", "pl", "pl-warsaw"),
        ("ワルシャワ", "pl", "pl-warsaw"),
        ("Ginebra", "ch", "ch-geneva"),
        ("Bâle", "ch", "ch-basel"),
        ("Le Caire", "eg", "eg-cairo"),
        ("Tóquio", "jp", "jp-tokyo"),
        ("Ciudad del Cabo", "za", "za-cape-town"),
        # A curated market's city.
        ("Múnich", "de", "munich"),
        ("Munique", "de", "munich"),
        ("ミュンヘン", "de", "munich"),
        ("Londres", "uk", "london"),
        ("Nueva York", "us", "new-york"),
        ("Pékin", "cn", "beijing"),
        ("La Haye", "nl", "the-hague"),
        # Countries.
        ("Allemagne", "de", None),
        ("Alemania", "de", None),
        ("ドイツ", "de", None),
        ("Irlande", "ie", None),
        ("Estados Unidos", "us", None),
        ("Suiza", "ch", None),
        ("Inde", "in", None),
        ("Corée du Sud", "kr", None),
    ],
)
def test_a_place_written_in_another_language_is_recognized(markets, text, market, city):
    assert _place(markets, text) == ([market], city)


@pytest.mark.parametrize(
    ("text", "market", "city"),
    [
        ("Munchen", "de", "munich"),  # the curated alias is "München"
        ("Zurich", "ch", "ch-zurich"),
        ("Zúrich", "ch", "ch-zurich"),
        ("Bogota", "co", "co-bogota"),
        ("Malmo", "se", "se-malmo"),
    ],
)
def test_a_name_with_or_without_its_accents_is_the_same_place(markets, text, market, city):
    assert _place(markets, text) == ([market], city)


@pytest.mark.parametrize(
    ("text", "market"),
    [
        # A longer name holding a shorter one is the longer name.
        ("Irlanda del Norte", "uk"),
        ("Belfast, Irlande du Nord", "uk"),
        ("北アイルランド", "uk"),
        ("インドネシア", "id"),
        ("ニュージャージー州", "us"),
        ("ニューメキシコ州", "us"),
        # ...including a country holding a city's name: a city outranks a
        # country, so the city used to win.
        ("ポルトガル", "pt"),
        ("罗马尼亚", "ro"),
        ("伯利兹", "bz"),
        ("Porto Rico", "pr"),
    ],
)
def test_a_name_inside_a_longer_name_is_the_longer_name(markets, text, market):
    assert market_plan.normalize_location(text, markets)["market_ids"] == [market]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Folded, "Münster" is the Irish province Munster: curated aliases
        # are not folded.
        ("Münster", []),
        ("Long Island, NY", ["us"]),  # German "Island" is Iceland
        ("Rhode Island", ["us"]),
        ("Granada, Spain", ["es"]),  # Spanish "Granada" is Grenada
        ("Salvador, Bahia", []),  # French "Salvador" is El Salvador
        ("Maurice", []),  # French for Mauritius, and a given name
        ("Vienne", []),  # French for Vienna, and a French department
        ("フルタイム", []),  # holds タイ, Thailand
        ("アルバイト", []),  # holds アルバ, Aruba
        ("machine learning", []),  # holds "Chine"
        ("Quebec", ["ca"]),  # the province; French "Québec" is also the city
    ],
)
def test_a_name_that_is_another_place_or_word_is_not_read_as_one(markets, text, expected):
    assert market_plan.normalize_location(text, markets)["market_ids"] == expected


def test_the_province_of_quebec_is_not_its_city(markets):
    assert market_plan.normalize_location("Quebec", markets)["city_id"] is None


def test_every_name_in_the_catalog_finds_its_own_place(markets):
    """Each name a place has, in any language, resolves back to that place.

    This is what catches a new name that collides with another place's.
    """
    known = {("ge", "en")}  # "Georgia" is the US state first; see [R3-13]
    wrong = []
    for market in markets["markets"]:
        for language, name in market["country_names"].items():
            placed = market_plan.normalize_location(name, markets)
            if (market["market_id"], language) in known:
                continue
            if placed["market_ids"][:1] != [market["market_id"]] or placed["city_id"]:
                wrong.append((market["market_id"], language, name, placed["market_ids"]))
        for city in market["cities"]:
            for language, name in city["names"].items():
                placed = market_plan.normalize_location(name, markets)
                if placed["city_id"] != city["city_id"]:
                    wrong.append((city["city_id"], language, name, placed["city_id"]))
    assert not wrong


@pytest.mark.parametrize(
    ("code", "language", "name"),
    [("IS", "de", "Island"), ("GD", "es", "Granada"), ("MU", "fr", "Maurice"),
     ("TH", "ja", "タイ"), ("AW", "ja", "アルバ"), ("JE", "ja", "ジャージー")],
)
def test_a_name_that_collides_is_left_out_of_the_catalog(code, language, name):
    payload = json.loads(
        (SKILL_ROOT / "references" / "geo_countries.json").read_text(encoding="utf-8")
    )
    country = next(c for c in payload["countries"] if c["country_code"] == code)
    assert (country.get("names") or {}).get(language) != name


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("Télétravail", "remote"),
        ("100% remoto", "remote"),
        ("フルリモート", "remote"),
        ("Híbrido - Madrid", "hybrid"),
        ("Hybride, Paris", "hybrid"),
        ("Presencial Lisboa", "onsite"),
    ],
)
def test_the_work_mode_is_read_in_the_markets_language(markets, text, mode):
    assert market_plan.normalize_location(text, markets)["work_mode"] == mode


@pytest.mark.parametrize(
    ("location", "market", "query"),
    [
        ("Varsovie", "pl", "Software Engineer jobs Warsaw"),
        ("Múnich", "de", "Softwareentwickler Stellenangebote München"),
        ("ロンドン", "uk", "Software Engineer jobs London"),
        ("Tóquio", "jp", "ソフトウェアエンジニア 求人 東京"),
    ],
)
def test_a_round_aimed_at_a_place_in_the_users_language_searches_there(
    location, market, query
):
    markets_, taxonomy = market_plan.load_resources()
    plan = market_plan.build_market_plan(
        {
            "cv_profile": {"preferred_roles": ["Software Engineer"], "skills": ["Python"]},
            "user_intent": {"locations": [location]},
        },
        markets=markets_,
        taxonomy=taxonomy,
        max_websearch_calls=6,
        multi_region_enabled=False,
    )

    assert plan["target_markets"] == [market]
    assert plan["needs_user_input"] is False
    assert plan["search_plan"][0]["query_string"] == query


def test_the_match_index_follows_the_catalog_it_was_built_for(markets):
    """The index is cached per catalog; a different catalog gets its own."""
    trimmed = {**markets, "markets": [m for m in markets["markets"] if m["market_id"] != "pl"]}

    assert market_plan.normalize_location("Varsovie", markets)["market_ids"] == ["pl"]
    assert market_plan.normalize_location("Varsovie", trimmed)["market_ids"] == []
    assert market_plan.normalize_location("Varsovie", markets)["market_ids"] == ["pl"]
