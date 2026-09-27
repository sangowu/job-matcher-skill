#!/usr/bin/env python3
"""Validate market resources and build a deterministic multi-region search plan.

The planner is an opt-in Phase A boundary. It does not call Web Search, mutate
the canonical job table, or enable the future regional-source pipeline.

Usage:
  python scripts/market_plan.py validate
  python scripts/market_plan.py plan < input.json
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable
from _jobutil import supported_markets
from _stdio import StdinUnavailable, read_stdin_text


SKILL_ROOT = Path(__file__).resolve().parent.parent
MARKETS_PATH = SKILL_ROOT / "references" / "markets.json"
ROLE_TAXONOMY_PATH = SKILL_ROOT / "references" / "role_taxonomy.json"
SOURCE_SEEDS_PATH = SKILL_ROOT / "references" / "source_seeds.json"
CONFIG_PATH = SKILL_ROOT / "config.json"

SCHEMA_VERSION = 1
SUPPORTED_MARKETS = supported_markets()
INTERNAL_LANGUAGES = ("en", "de", "zh-Hans")
DISCOVERY_ROUTES = {"agent_web_search"}
SOURCE_TYPES = {"open_web"}
# Titles one CV role is searched under, and how many of them a generalization
# may claim. Three is the ceiling the whole expansion is held to, so a role
# that generalizes twice keeps one own-family title, not none.
MAX_ROLE_VARIANTS = 3
MAX_GENERALIZED_FAMILIES = 2
_LANGUAGE_ALIASES = {
    "en": "en",
    "english": "en",
    "英文": "en",
    "英语": "en",
    "de": "de",
    "de-de": "de",
    "german": "de",
    "deutsch": "de",
    "德语": "de",
    "zh": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh_hans": "zh-Hans",
    "zh-hans": "zh-Hans",
    "chinese": "zh-Hans",
    "simplified chinese": "zh-Hans",
    "中文": "zh-Hans",
    "简体中文": "zh-Hans",
}
_REMOTE_TERMS = ("remote", "远程", "home office", "homeoffice")
_HYBRID_TERMS = ("hybrid", "混合办公", "hybridarbeit")
_ONSITE_TERMS = ("onsite", "on-site", "现场办公", "vor ort")


class MarketPlanError(ValueError):
    """Raised when a versioned market resource violates its contract."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MarketPlanError(f"cannot read valid JSON: {path.name}") from error
    if not isinstance(payload, dict):
        raise MarketPlanError(f"{path.name} must contain a JSON object")
    return payload


def _strings(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise MarketPlanError(f"{field} must be a {'possibly empty ' if allow_empty else ''}list")
    output: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise MarketPlanError(f"{field} must contain non-empty strings")
        output.append(item.strip())
    return output


def _require_unique(values: Iterable[str], field: str) -> None:
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key in seen:
            raise MarketPlanError(f"duplicate {field}: {value}")
        seen.add(key)


def normalize_language(value: Any) -> str | None:
    """Normalize a boundary language value to the Phase A internal codes."""
    raw = str(value or "").strip()
    if not raw:
        return None
    return _LANGUAGE_ALIASES.get(raw.casefold())


def _validate_foreign_areas(value: Any, markets: list[Any]) -> None:
    """Check the negative catalog, including that it stays negative.

    A name that is also one of our own country aliases would read every
    posting in that market as foreign, which is the failure this catalog
    exists to prevent, pointed the other way. Codes are exempt: "DE" is
    Delaware and Germany both, and the corroboration rule in
    `_foreign_qualifier` is what settles it.
    """
    if value is None:
        return
    if not isinstance(value, dict):
        raise MarketPlanError("foreign_administrative_areas must be an object")
    names = _strings(value.get("names"), "foreign_administrative_areas.names")
    codes = _strings(value.get("codes"), "foreign_administrative_areas.codes")
    _require_unique(names, "foreign administrative area name")
    _require_unique(codes, "foreign administrative area code")
    for code in codes:
        if not re.fullmatch(r"[A-Z]{2}", code):
            raise MarketPlanError(
                f"foreign administrative area code must be two capitals: {code}"
            )
    owned = {
        _normalize_text(alias)
        for market in markets
        if isinstance(market, dict)
        for alias in (market.get("country_aliases") or [])
    }
    clashing = sorted({name for name in names if _normalize_text(name) in owned})
    if clashing:
        raise MarketPlanError(
            "foreign administrative area name is a supported market alias: "
            + ", ".join(clashing)
        )
    # Cities too, and this one is what stands in the way of a new market rather
    # than a mislabelled one. This list is a closed reverse-catalog of the
    # places no supported market covers -- 50 states, 13 provinces, DC -- and it
    # reads "New York" as foreign because no market claimed it. Add a US market
    # without pruning it and `normalize_location("New York")` still answers
    # `location_type: foreign` with no market: the market exists, its city is
    # listed, and every posting in it is dropped, silently. Named here so the
    # contradiction is an error at load rather than an absence in the results.
    towns = {
        _normalize_text(alias)
        for market in markets
        if isinstance(market, dict)
        for city in (market.get("cities") or [])
        if isinstance(city, dict)
        for alias in [city.get("name"), *(city.get("aliases") or [])]
        if isinstance(alias, str)
    }
    local = sorted({name for name in names if _normalize_text(name) in towns})
    if local:
        raise MarketPlanError(
            "foreign administrative area name is a supported market's own city: "
            + ", ".join(local)
        )


def validate_markets(
    payload: dict[str, Any], *, known_source_ids: set[str] | None = None
) -> dict[str, Any]:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise MarketPlanError("markets.json schema_version must be 1")
    markets = payload.get("markets")
    if not isinstance(markets, list) or not markets:
        raise MarketPlanError("markets must be a non-empty list")
    _validate_foreign_areas(payload.get("foreign_administrative_areas"), markets)
    market_ids: list[str] = []
    city_ids: list[str] = []
    template_ids: list[str] = []
    for market in markets:
        if not isinstance(market, dict):
            raise MarketPlanError("each market must be an object")
        market_id = str(market.get("market_id") or "")
        if market_id not in SUPPORTED_MARKETS:
            raise MarketPlanError(f"unsupported market_id: {market_id or '<empty>'}")
        market_ids.append(market_id)
        if not isinstance(market.get("country"), str) or not market["country"].strip():
            raise MarketPlanError(f"{market_id}.country must be a non-empty string")
        _strings(market.get("country_aliases"), f"{market_id}.country_aliases")
        country_names = market.get("country_names")
        if not isinstance(country_names, dict):
            raise MarketPlanError(f"{market_id}.country_names must be an object")
        for language in INTERNAL_LANGUAGES:
            if not isinstance(country_names.get(language), str) or not country_names[language]:
                raise MarketPlanError(f"{market_id}.country_names.{language} is required")
        languages = _strings(
            market.get("default_search_languages"),
            f"{market_id}.default_search_languages",
        )
        if any(language not in INTERNAL_LANGUAGES for language in languages):
            raise MarketPlanError(f"{market_id} uses a non-canonical language code")
        _require_unique(languages, f"{market_id} search language")

        cities = market.get("cities")
        if not isinstance(cities, list) or not cities:
            raise MarketPlanError(f"{market_id}.cities must be a non-empty list")
        for city in cities:
            if not isinstance(city, dict):
                raise MarketPlanError(f"{market_id}.cities entries must be objects")
            city_id = str(city.get("city_id") or "")
            if not re.fullmatch(r"[a-z][a-z0-9-]*", city_id):
                raise MarketPlanError(f"invalid city_id: {city_id or '<empty>'}")
            city_ids.append(city_id)
            if not isinstance(city.get("name"), str) or not city["name"].strip():
                raise MarketPlanError(f"{city_id}.name must be a non-empty string")
            aliases = _strings(city.get("aliases"), f"{city_id}.aliases")
            _require_unique(aliases, f"{city_id} alias")
            _strings(
                city.get("administrative_areas"),
                f"{city_id}.administrative_areas",
                allow_empty=True,
            )
            names = city.get("names")
            if not isinstance(names, dict):
                raise MarketPlanError(f"{city_id}.names must be an object")
            for language in INTERNAL_LANGUAGES:
                if not isinstance(names.get(language), str) or not names[language]:
                    raise MarketPlanError(f"{city_id}.names.{language} is required")

        source_ids = _strings(
            market.get("source_ids"), f"{market_id}.source_ids", allow_empty=True
        )
        _require_unique(source_ids, f"{market_id} source_id")
        if known_source_ids is not None:
            unknown = sorted(set(source_ids) - known_source_ids)
            if unknown:
                raise MarketPlanError(
                    f"{market_id} references unknown source_id: {', '.join(unknown)}"
                )

        templates = market.get("query_templates")
        if not isinstance(templates, list) or not templates:
            raise MarketPlanError(f"{market_id}.query_templates must be a non-empty list")
        template_languages: set[str] = set()
        for template in templates:
            if not isinstance(template, dict):
                raise MarketPlanError(f"{market_id} query template must be an object")
            template_id = str(template.get("template_id") or "")
            template_ids.append(template_id)
            language = str(template.get("language") or "")
            template_languages.add(language)
            if language not in languages:
                raise MarketPlanError(f"{template_id} language is not enabled for {market_id}")
            if template.get("source_type") not in SOURCE_TYPES:
                raise MarketPlanError(f"{template_id} has invalid source_type")
            if template.get("discovery_route") not in DISCOVERY_ROUTES:
                raise MarketPlanError(f"{template_id} has invalid discovery_route")
            text = str(template.get("template") or "")
            if "{role}" not in text or "{location}" not in text:
                raise MarketPlanError(f"{template_id} must contain role and location placeholders")
        if template_languages != set(languages):
            raise MarketPlanError(f"{market_id} requires one query template per search language")

    _require_unique(market_ids, "market_id")
    if not market_ids:
        raise MarketPlanError("markets.json must define at least one market")
    _validate_administrative_areas(markets, payload.get("foreign_administrative_areas"))
    _require_unique(city_ids, "city_id")
    _require_unique(template_ids, "template_id")
    return payload


def validate_role_taxonomy(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise MarketPlanError("role_taxonomy.json schema_version must be 1")
    families = payload.get("role_families")
    if not isinstance(families, list) or not families:
        raise MarketPlanError("role_families must be a non-empty list")
    family_ids: list[str] = []
    for family in families:
        if not isinstance(family, dict):
            raise MarketPlanError("role family entries must be objects")
        family_id = str(family.get("role_family_id") or "")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", family_id):
            raise MarketPlanError(f"invalid role_family_id: {family_id or '<empty>'}")
        family_ids.append(family_id)
        for group_name in ("titles", "synonyms"):
            groups = family.get(group_name)
            if not isinstance(groups, dict) or set(groups) != set(INTERNAL_LANGUAGES):
                raise MarketPlanError(
                    f"{family_id}.{group_name} must define en, de, and zh-Hans"
                )
            for language, values in groups.items():
                phrases = _strings(values, f"{family_id}.{group_name}.{language}")
                _require_unique(phrases, f"{family_id} {language} {group_name}")
    _require_unique(family_ids, "role_family_id")
    _validate_generalizations(families, family_ids)
    _validate_match_vocabulary(payload, families, family_ids)
    return payload


def _validate_match_vocabulary(
    payload: dict[str, Any], families: list[Any], family_ids: list[str]
) -> None:
    """Check the terms `job_prefilter` recognizes titles by.

    They live here because a round used to search by one vocabulary and filter
    by another: this file decided what was searched for and a table inside
    `job_prefilter` decided what was kept, with family ids that did not even
    agree. `match_only_families` carry terms and no titles -- recognized in a
    posting, never searched for.
    """
    match_only = payload.get("match_only_families")
    if match_only is None:
        match_only = []
    if not isinstance(match_only, list):
        raise MarketPlanError("match_only_families must be a list")
    seen = list(family_ids)
    for group in [*families, *match_only]:
        if not isinstance(group, dict):
            raise MarketPlanError("match_only_families entries must be objects")
        family_id = str(group.get("role_family_id") or "")
        if group in match_only:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", family_id):
                raise MarketPlanError(f"invalid role_family_id: {family_id or '<empty>'}")
            seen.append(family_id)
            if not group.get("match_terms"):
                raise MarketPlanError(f"{family_id} exists only to match, so it needs match_terms")
            if set(group) - {"role_family_id", "match_terms", "match_tokens"}:
                raise MarketPlanError(f"{family_id} carries fields a match-only family cannot have")
        for key in ("match_terms", "match_tokens"):
            if key not in group:
                continue
            values = _strings(group[key], f"{family_id}.{key}")
            _require_unique(values, f"{family_id} {key}")
            if key == "match_tokens" and any(" " in value for value in values):
                raise MarketPlanError(
                    f"{family_id}.match_tokens must be single tokens, not phrases"
                )
    _require_unique(seen, "role_family_id")


def _validate_generalizations(families: list[Any], family_ids: list[str]) -> None:
    """Check the optional `generalizes_to` edges between role families.

    An edge is what carries a CV from the role it names to an adjacent one it
    can also be read for -- an AI engineer with a Python and FastAPI stack to
    a backend engineer. The skills are the gate: without them every CV would
    be generalized the same way, which is a wider search, not an adaptive one.
    """
    known = set(family_ids)
    for family in families:
        family_id = family["role_family_id"]
        targets = family.get("generalizes_to")
        if targets is None:
            continue
        if not isinstance(targets, list) or not targets:
            raise MarketPlanError(f"{family_id}.generalizes_to must be a non-empty list")
        seen: list[str] = []
        for target in targets:
            if not isinstance(target, dict):
                raise MarketPlanError(f"{family_id}.generalizes_to entries must be objects")
            target_id = str(target.get("role_family_id") or "")
            if target_id not in known:
                raise MarketPlanError(
                    f"{family_id} generalizes to an unknown family: {target_id or '<empty>'}"
                )
            if target_id == family_id:
                raise MarketPlanError(f"{family_id} cannot generalize to itself")
            seen.append(target_id)
            _strings(target.get("skills"), f"{family_id}.generalizes_to.{target_id}.skills")
        _require_unique(seen, f"{family_id} generalizes_to target")


def _validate_administrative_areas(markets: list[Any], foreign: Any) -> None:
    """Check the areas a market claims as its own.

    An area answers "which market does this belong to", so two answers is no
    answer: a name claimed by two markets, or by a market and the foreign
    catalog at once, would be decided by iteration order. The foreign catalog
    is what is left over -- places no market covers -- so the moment a market
    covers one, it has to leave that list.
    """
    owner: dict[str, str] = {}
    foreign_names = set()
    foreign_codes = set()
    if isinstance(foreign, dict):
        foreign_names = {_normalize_text(name) for name in foreign.get("names", ())}
        foreign_codes = {str(code).casefold() for code in foreign.get("codes", ())}
    for market in markets:
        if not isinstance(market, dict):
            continue
        market_id = str(market.get("market_id") or "")
        areas = market.get("administrative_areas")
        if areas is None:
            continue
        if not isinstance(areas, dict) or set(areas) - {"names", "codes"}:
            raise MarketPlanError(f"{market_id}.administrative_areas must hold names and codes")
        names = _strings(areas.get("names"), f"{market_id}.administrative_areas.names")
        codes = _strings(areas.get("codes"), f"{market_id}.administrative_areas.codes")
        _require_unique(names, f"{market_id} administrative area name")
        _require_unique(codes, f"{market_id} administrative area code")
        for code in codes:
            if not re.fullmatch(r"[A-Z]{2}", code):
                raise MarketPlanError(
                    f"{market_id} administrative area code must be two capitals: {code}"
                )
        for value, bucket in ((names, foreign_names), (codes, foreign_codes)):
            for item in value:
                key = _normalize_text(item) if bucket is foreign_names else item.casefold()
                if key in bucket:
                    raise MarketPlanError(
                        f"{market_id} claims an administrative area the foreign catalog also"
                        f" claims: {item}"
                    )
                if key in owner and owner[key] != market_id:
                    raise MarketPlanError(
                        f"administrative area claimed by {owner[key]} and {market_id}: {item}"
                    )
                owner[key] = market_id


def load_resources(
    markets_path: Path = MARKETS_PATH,
    taxonomy_path: Path = ROLE_TAXONOMY_PATH,
    *,
    known_source_ids: set[str] | None = None,
    source_seeds_path: Path = SOURCE_SEEDS_PATH,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if known_source_ids is None and source_seeds_path.exists():
        seeds = _read_json(source_seeds_path)
        sources = seeds.get("sources")
        if not isinstance(sources, list):
            raise MarketPlanError("source_seeds.json sources must be a list")
        known_source_ids = {
            str(source.get("source_id"))
            for source in sources
            if isinstance(source, dict) and source.get("source_id")
        }
    markets = validate_markets(_read_json(markets_path), known_source_ids=known_source_ids)
    taxonomy = validate_role_taxonomy(_read_json(taxonomy_path))
    return markets, taxonomy


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"[\s,;/_|]+", " ", text)


def _work_mode(value: str) -> str:
    normalized = _normalize_text(value)
    if any(term in normalized for term in _HYBRID_TERMS):
        return "hybrid"
    if any(term in normalized for term in _REMOTE_TERMS):
        return "remote"
    if any(term in normalized for term in _ONSITE_TERMS):
        return "onsite"
    return "unspecified"


def _foreign_qualifier(raw: str, normalized: str, markets: dict[str, Any]) -> str | None:
    """The US state or Canadian province this location names, if any.

    A city name is not unique to a country. "Dublin, OH", "Dublin, CA",
    "Berlin, CT" and "London, ON" every one resolved to Ireland, Germany and
    the United Kingdom with `confidence: exact`, because the catalog holds no
    US or Canadian market and so had nothing to read the qualifier against --
    the city alias won alone. `filter_to_markets` is the last thing standing
    between a board's world and a market-scoped round, and it asks this
    function, so an Ohio posting entered an Irish report as an exact match.
    The exposure grows with `board_harvest.py`, which adds US-heavy boards.

    This is a negative catalog, and it is closed: fifty states, thirteen
    provinces and DC, a set that does not change. Missing an entry leaves
    today's behaviour for that entry rather than producing a new wrong answer,
    which is the opposite risk profile from the remote-scope catalog that was
    removed in v2.4.0 -- there the default was a confident "global", here the
    default is what the code already does.

    A qualifier only decides when the text names none of the supported
    markets: "Berlin, DE" is Delaware's code and Germany's alias at once, and
    "United Kingdom; Dublin; United States; New York" is a genuinely
    multi-country posting whose Dublin is Irish. Corroboration wins.
    """
    return _administrative_qualifier(raw, normalized, markets)[1]


def _administrative_area_match(catalog, segment: str) -> str | None:
    """The area this one segment of a location names, inside one catalog.

    A trailing ZIP ("Dublin, OH 43017") is dropped rather than read, and a name
    is matched inside the segment so "Massachusetts Area" still counts.
    """
    if not isinstance(catalog, dict):
        return None
    text = _normalize_text(re.sub(r"\b\d[\w-]*\b", " ", segment))
    if not text:
        return None
    for name in catalog.get("names", ()):
        if _alias_matches(str(name), text):
            return _normalize_text(name)
    # A bare two-letter code is only a place where a place belongs. "IN" is
    # Indiana in ", IN" and the English word in "Hybrid work in Dublin", and only
    # the segment tells them apart.
    if text in {str(code).casefold() for code in catalog.get("codes", ())}:
        return text
    return None


def _administrative_qualifier(
    raw: str, normalized: str, markets: dict
) -> tuple[str | None, str | None]:
    """Whose administrative area this names: a market of ours, or nobody's.

    The catalog used to answer only "foreign", which was right while no market
    covered the Americas: "Dublin, OH" had to be refused and there was no market
    to give it to. A US market makes those fifty names that market's own -- Ohio
    stops being somewhere else and becomes a place a round can be scoped to --
    while the thirteen Canadian provinces stay foreign, because no market covers
    them. An area cannot be both, so `validate_markets` refuses a catalog that
    claims one twice.

    Read in the order the text writes them, because a location can name two:
    "Toronto, ON, CA" is Ontario and California at once, and asking every market
    before the foreign catalog made that a US posting -- catalog precedence
    deciding what the text had already settled. The first segment that resolves
    wins.
    """
    for segment in re.split(r"[;,/|()]", str(raw)):
        for market in markets.get("markets", ()):
            if not isinstance(market, dict):
                continue
            hit = _administrative_area_match(
                market.get("administrative_areas"), segment
            )
            if hit:
                return str(market.get("market_id")), hit
        hit = _administrative_area_match(
            markets.get("foreign_administrative_areas"), segment
        )
        if hit:
            return None, hit
    return None, None

def _named_markets(normalized: str, markets: dict[str, Any]) -> set[str]:
    """Which markets the text names by country, not by city."""
    return {
        market["market_id"]
        for market in markets["markets"]
        for alias in market["country_aliases"]
        if _alias_matches(alias, normalized)
    }


def _names_supported_market(normalized: str, markets: dict[str, Any]) -> bool:
    return bool(_named_markets(normalized, markets))


def _alias_matches(alias: str, normalized_value: str) -> bool:
    alias_normalized = _normalize_text(alias)
    if alias_normalized == normalized_value:
        return True
    if len(alias_normalized) <= 3 and alias_normalized.isascii():
        return alias_normalized in normalized_value.split()
    return alias_normalized in normalized_value


def normalize_location(value: Any, markets: dict[str, Any]) -> dict[str, Any]:
    """Map a city/country/remote expression without guessing unknown places."""
    raw = str(value or "").strip()
    if not raw:
        return {
            "input": raw,
            "market_ids": [],
            "city_id": None,
            "location_id": None,
            "location_type": "unknown",
            "remote_scope": None,
            "work_mode": "unspecified",
            "confidence": "unknown",
            "canonical_name": None,
            "names": {},
        }
    normalized = _normalize_text(raw)
    mode = _work_mode(raw)
    owner, qualifier = _administrative_qualifier(raw, normalized, markets)
    named = _named_markets(normalized, markets)
    restrict_to: str | None = None
    if qualifier and owner is not None and not named - {owner}:
        # The qualifier decides which market this belongs to. Restricting rather
        # than short-circuiting keeps the city when the two agree: "New York, NY"
        # is still the city, and "Dublin, OH" is the market without one, because
        # the Irish city that matched is not in the market the qualifier names.
        #
        # Corroboration is read per market, not as a yes or no. "Dublin, Ohio,
        # United States" names a market by country -- the very market the
        # qualifier already named -- and reading that as "the text corroborates
        # something, so stand down" handed the posting back to the Irish city
        # alias. Only a *different* market in the text stands the qualifier
        # down, which is what settles "Berlin, DE" as Germany over Delaware.
        restrict_to = owner
    if qualifier and restrict_to is None and not named:
        # `foreign`, not `unknown`: "this names a place outside every market
        # we serve" and "we have never heard of this place" are different
        # answers, and a caller weighing a location somebody else normalized
        # needs to tell them apart -- one contradicts the claim, the other
        # only fails to corroborate it. `market_ids` is empty either way, so
        # everything that reads that field is unaffected.
        return {
            "input": raw,
            "market_ids": [],
            "city_id": None,
            "location_id": None,
            "location_type": "foreign",
            "remote_scope": None,
            "work_mode": mode,
            "confidence": "unknown",
            "canonical_name": None,
            "names": {},
        }

    def _position(alias_norm: str) -> int:
        """Where this alias sits in the text; unfound aliases sort to the end."""
        found = normalized.find(alias_norm)
        return len(normalized) if found < 0 else found

    location_matches: list[
        tuple[int, int, dict[str, Any], dict[str, Any] | None, int]
    ] = []
    for market in markets["markets"]:
        for city in market["cities"]:
            aliases = [*city["aliases"], *city["administrative_areas"]]
            for alias in aliases:
                alias_norm = _normalize_text(alias)
                if _alias_matches(alias, normalized):
                    location_matches.append(
                        (len(alias_norm), 2, market, city, _position(alias_norm))
                    )
        for alias in market["country_aliases"]:
            alias_norm = _normalize_text(alias)
            if _alias_matches(alias, normalized):
                location_matches.append(
                    (len(alias_norm), 1, market, None, _position(alias_norm))
                )

    if restrict_to is not None:
        location_matches = [
            item for item in location_matches if item[2]["market_id"] == restrict_to
        ]
        if not location_matches:
            market = next(
                item for item in markets["markets"]
                if item["market_id"] == restrict_to
            )
            return {
                "input": raw,
                "market_ids": [restrict_to],
                "city_id": None,
                "location_id": restrict_to,
                # The market is known and the city is not: this catalog holds
                # six US cities and the country has thousands.
                "location_type": "country",
                "remote_scope": None,
                "work_mode": mode,
                "confidence": "country",
                "canonical_name": market["country"],
                "names": market["country_names"],
            }

    if location_matches:
        # Specificity first, then the longest alias inside that tier. Length
        # used to come first, so which reading won depended on spelling rather
        # than geography: "Frankfurt, Germany" kept its city because
        # "frankfurt" is longer than "germany", while "Dublin, Ireland" lost
        # its city to a country alias one character longer. Every city whose
        # name is shorter than its country's was being thrown away.
        # Then the earliest one named, so a posting listing several places is
        # read the way it is written rather than by catalog iteration order:
        # "Dublin, Ireland; London, England" is a Dublin job that is also open
        # in London. Alias length only breaks a remaining tie.
        _, confidence_rank, market, city, _ = max(
            location_matches, key=lambda item: (item[1], -item[4], item[0])
        )
        # Every market the text names, not only the winner's. The field is
        # plural and its readers treat it as a set, but one match was reported:
        # "Dublin, Ireland; London, England" resolved to the United Kingdom
        # alone, attributing a plainly Dublin job to the wrong market. The
        # winner stays first, so the primary reading is still readable off the
        # front of the list.
        # A shorter alias matched entirely inside a longer one is that longer
        # name, not a second place: "Northern Ireland" contains "Ireland", and
        # counting both would put a Belfast job in the Republic.
        def _subsumed(item: tuple[int, int, Any, Any, int]) -> bool:
            length, _, _, _, start = item
            return any(
                other[0] > length
                and other[4] <= start
                and other[4] + other[0] >= start + length
                for other in location_matches
            )

        others = sorted(
            {
                item[2]["market_id"]
                for item in location_matches
                if not _subsumed(item)
            }
            - {market["market_id"]}
        )
        return {
            "input": raw,
            "market_ids": [market["market_id"], *others],
            "city_id": city["city_id"] if city else None,
            "location_id": city["city_id"] if city else market["market_id"],
            "location_type": "city" if city else "country",
            # Kept in the shape for the envelope contract, never populated:
            # which jurisdictions a remote posting may be worked from is not
            # modelled here. See docs/roadmap.md.
            "remote_scope": None,
            "work_mode": mode,
            "confidence": "exact" if confidence_rank == 2 else "country",
            "canonical_name": city["name"] if city else market["country"],
            "names": city["names"] if city else market["country_names"],
        }

    return {
        "input": raw,
        "market_ids": [],
        "city_id": None,
        "location_id": None,
        "location_type": "unknown",
        "remote_scope": None,
        "work_mode": mode,
        "confidence": "unknown",
        "canonical_name": None,
        "names": {},
    }


def location_matches_market(location: Any, market_id: str, markets: dict[str, Any]) -> bool | None:
    normalized = normalize_location(location, markets)
    if normalized["confidence"] == "unknown":
        return None
    return market_id in normalized["market_ids"]


def _dedupe_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        key = value.strip().casefold()
        if key not in seen:
            seen.add(key)
            output.append(value.strip())
    return output


def _role_lookup(taxonomy: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    rows: list[tuple[str, str, dict[str, Any]]] = []
    for family in taxonomy["role_families"]:
        for groups in (family["titles"], family["synonyms"]):
            for phrases in groups.values():
                for phrase in phrases:
                    normalized = _normalize_text(phrase)
                    rows.append((normalized, family["role_family_id"], family))
    return sorted(rows, key=lambda row: len(row[0]), reverse=True)


def resolve_role_family(role: Any, taxonomy: dict[str, Any]) -> str | None:
    normalized = _normalize_text(role)
    if not normalized or normalized == "ai":
        return None
    for phrase, family_id, _ in _role_lookup(taxonomy):
        if phrase == normalized or (len(phrase) >= 4 and phrase in normalized):
            return family_id
    return None


def _family_by_id(family_id: str, taxonomy: dict[str, Any]) -> dict[str, Any]:
    return next(
        item for item in taxonomy["role_families"]
        if item["role_family_id"] == family_id
    )


def _role_variants(role: str, language: str, taxonomy: dict[str, Any]) -> list[str]:
    family_id = resolve_role_family(role, taxonomy)
    if family_id is None:
        return [role]
    family = _family_by_id(family_id, taxonomy)
    return _dedupe_strings([*family["titles"][language], *family["synonyms"][language]])


def _generalized_families(
    role: str, taxonomy: dict[str, Any], skills: set[str]
) -> list[str]:
    """The adjacent families this CV's skills actually support, best first.

    Ordered by how many of the target's gate skills the CV carries, so a CV
    with a whole backend stack and one stray SQL line generalizes to backend
    first. Ties keep the catalog's own order, which is the author's ranking.
    """
    family_id = resolve_role_family(role, taxonomy)
    if family_id is None or not skills:
        return []
    targets = _family_by_id(family_id, taxonomy).get("generalizes_to") or []
    scored: list[tuple[int, int, str]] = []
    for position, target in enumerate(targets):
        hits = len({_normalize_text(skill) for skill in target["skills"]} & skills)
        if hits:
            scored.append((-hits, position, target["role_family_id"]))
    scored.sort()
    return [target_id for _, _, target_id in scored[:MAX_GENERALIZED_FAMILIES]]


def _variants_for_role(
    role: str, language: str, taxonomy: dict[str, Any], skills: set[str]
) -> list[tuple[str, str | None]]:
    """The titles this role is searched under, as `(title, role_family_id)`.

    At most `MAX_ROLE_VARIANTS` of them, the role's own family first, because
    the first one is what a truncated budget keeps. Each adjacent family the
    CV's skills reached contributes its leading title and costs one slot from
    the own-family variants, never the first: the answer to "what is this CV"
    is not allowed to be crowded out by the answer to "what else could it be".
    """
    own_family_id = resolve_role_family(role, taxonomy)
    own = [(title, own_family_id) for title in _role_variants(role, language, taxonomy)]
    generalized = [
        (_family_by_id(target_id, taxonomy)["titles"][language][0], target_id)
        for target_id in _generalized_families(role, taxonomy, skills)
    ]
    own_slots = max(1, MAX_ROLE_VARIANTS - len(generalized))
    merged: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for title, family_id in [*own[:own_slots], *generalized]:
        key = _normalize_text(title)
        if key in seen:
            continue
        seen.add(key)
        merged.append((title, family_id))
    return merged[:MAX_ROLE_VARIANTS]


def _market_map(markets: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {market["market_id"]: market for market in markets["markets"]}


def _pick_report_language(
    user_intent: dict[str, Any], cv_profile: dict[str, Any], warnings: list[str]
) -> str:
    candidates = (
        user_intent.get("report_language"),
        cv_profile.get("report_language"),
        cv_profile.get("cv_language"),
        cv_profile.get("search_language"),
        "en",
    )
    for candidate in candidates:
        if not candidate:
            continue
        normalized = normalize_language(candidate)
        if normalized:
            return normalized
        warnings.append(f"unsupported report language ignored: {candidate}")
    return "en"


def _search_languages(
    market: dict[str, Any], user_intent: dict[str, Any], warnings: list[str]
) -> list[str]:
    raw_explicit = user_intent.get("search_languages")
    if raw_explicit is None:
        raw_explicit = user_intent.get("job_languages")
    if raw_explicit is None:
        return list(market["default_search_languages"])
    explicit = _dedupe_strings(raw_explicit)
    normalized = [normalize_language(value) for value in explicit]
    invalid = [value for value, code in zip(explicit, normalized) if code is None]
    if invalid:
        warnings.append(f"unsupported search language ignored: {', '.join(invalid)}")
    allowed = [
        code for code in normalized
        if code is not None and code in market["default_search_languages"]
    ]
    return list(dict.fromkeys(allowed))


def _location_for_market(
    details: list[dict[str, Any]], market_id: str, language: str, market: dict[str, Any]
) -> str:
    for detail in details:
        if market_id not in detail["market_ids"]:
            continue
        if detail["names"]:
            return str(detail["names"].get(language) or detail["canonical_name"])
    return str(market["country_names"].get(language) or market["country"])


def _query_candidates(
    market: dict[str, Any],
    languages: list[str],
    roles: list[str],
    locations: list[dict[str, Any]],
    taxonomy: dict[str, Any],
    skills: set[str],
) -> list[dict[str, Any]]:
    """Every query this market is worth running, the first variants first.

    Variant-major, so slicing this list anywhere keeps each role's leading
    title ahead of its second one. The Web Search budget slices it; the
    browser channel reads the whole thing.
    """
    templates = {item["language"]: item for item in market["query_templates"]}
    result: list[dict[str, Any]] = []
    seen_queries: set[str] = set()
    for variant_index in range(MAX_ROLE_VARIANTS):
        for language in languages:
            template = templates[language]
            location = _location_for_market(
                locations, market["market_id"], language, market
            )
            for role in roles:
                variants = _variants_for_role(role, language, taxonomy, skills)
                if variant_index >= len(variants):
                    continue
                title, family_id = variants[variant_index]
                query = template["template"].format(role=title, location=location)
                query_key = _normalize_text(query)
                if query_key in seen_queries:
                    continue
                seen_queries.add(query_key)
                result.append({
                    "market_id": market["market_id"],
                    "language": language,
                    "role": title,
                    "role_family_id": family_id,
                    "location": location,
                    "query_string": query,
                    "query_template_id": template["template_id"],
                    "source_type": template["source_type"],
                    "discovery_route": template["discovery_route"],
                })
    return result


def build_market_plan(
    request: dict[str, Any],
    *,
    markets: dict[str, Any] | None = None,
    taxonomy: dict[str, Any] | None = None,
    max_websearch_calls: int | None = None,
    multi_region_enabled: bool | None = None,
) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise MarketPlanError("plan input must be a JSON object")
    if markets is None or taxonomy is None:
        markets, taxonomy = load_resources()
    cv_profile = request.get("cv_profile") or {}
    user_intent = request.get("user_intent") or {}
    if not isinstance(cv_profile, dict) or not isinstance(user_intent, dict):
        raise MarketPlanError("cv_profile and user_intent must be objects")
    warnings: list[str] = []
    report_language = _pick_report_language(user_intent, cv_profile, warnings)

    # Reported as `roles_source`, the way locations already report theirs.
    # Which of these three answered is the whole question when the searches a
    # round runs and the filter it applies them to disagree.
    roles = _dedupe_strings(user_intent.get("roles"))
    roles_source = "user_intent"
    if not roles:
        roles = _dedupe_strings(cv_profile.get("target_roles"))
        roles_source = "cv_target"
    if not roles:
        roles = _dedupe_strings(cv_profile.get("preferred_roles"))
        roles_source = "cv_preferred"
    if not roles:
        roles_source = "none"

    # The CV's own stack decides which adjacent roles it is generalized to, so
    # two candidates who both write "AI Engineer" are not searched the same way.
    skills = {_normalize_text(skill) for skill in _dedupe_strings(cv_profile.get("skills"))}
    generalized_roles = _dedupe_strings([
        _family_by_id(family_id, taxonomy)["titles"]["en"][0]
        for role in roles
        for family_id in _generalized_families(role, taxonomy, skills)
    ])

    explicit_locations = "locations" in user_intent
    location_values = _dedupe_strings(user_intent.get("locations"))
    location_source = "user_intent"
    if not explicit_locations:
        location_values = _dedupe_strings(cv_profile.get("target_locations"))
        location_source = "cv_target"
        if not location_values:
            location_values = _dedupe_strings(cv_profile.get("preferred_locations"))
            location_source = "cv_legacy"
        if not location_values and isinstance(cv_profile.get("current_location"), str):
            location_values = _dedupe_strings([cv_profile["current_location"]])
            location_source = "cv_current_fallback"

    details = [normalize_location(value, markets) for value in location_values]
    unknown_locations = [detail["input"] for detail in details if not detail["market_ids"]]
    if unknown_locations:
        warnings.append(f"unrecognized target location: {', '.join(unknown_locations)}")
    recognized = [detail for detail in details if detail["market_ids"]]
    target_markets: list[str] = []
    for detail in recognized:
        for market_id in detail["market_ids"]:
            if market_id not in target_markets:
                target_markets.append(market_id)

    needs_user_input = False
    if not roles:
        warnings.append("target role is required")
        needs_user_input = True
    if not location_values:
        warnings.append("target location is required")
        needs_user_input = True
    elif not target_markets:
        needs_user_input = True

    config = _read_json(CONFIG_PATH) if (
        max_websearch_calls is None or multi_region_enabled is None
    ) else {}
    if max_websearch_calls is None:
        max_websearch_calls = int(config.get("max_websearch_calls", 6))
    if multi_region_enabled is None:
        multi_region_enabled = config.get("multi_region_enabled", False) is True
    budget = min(6, max(0, int(max_websearch_calls)))
    if len(target_markets) > budget:
        warnings.append("Web Search budget cannot cover every requested market")
        needs_user_input = True

    market_by_id = _market_map(markets)
    languages_by_market: dict[str, list[str]] = {}
    for market_id in target_markets:
        languages = _search_languages(market_by_id[market_id], user_intent, warnings)
        languages_by_market[market_id] = languages
        if not languages:
            warnings.append(f"explicit search languages do not match market {market_id}")
            needs_user_input = True

    # One budget used to cut this list, and every channel read what survived.
    # `max_websearch_calls` is a Web Search budget: the browser channel spends
    # none of it, yet a 3-market round left it exactly one title per role
    # because the cut fell at six. The expansion is built in full here, and
    # only the Web Search slice is capped.
    expanded: list[dict[str, Any]] = []
    if not needs_user_input:
        queues = {
            market_id: _query_candidates(
                market_by_id[market_id],
                languages_by_market[market_id],
                roles,
                recognized,
                taxonomy,
                skills,
            )
            for market_id in target_markets
        }
        cursor = {market_id: 0 for market_id in target_markets}
        while True:
            added = False
            for market_id in target_markets:
                index = cursor[market_id]
                if index >= len(queues[market_id]):
                    continue
                expanded.append(queues[market_id][index])
                cursor[market_id] += 1
                added = True
            if not added:
                break
    search_plan = expanded[:budget]
    # The browser channel needs the title, the language and the location; the
    # query string and template belong to Web Search. Kept separate so the
    # pool the browser reads does not cost the orchestrator a second copy of
    # every row it already has.
    role_plan = [
        {key: row[key] for key in ("market_id", "language", "role", "location")}
        for row in expanded
    ]

    all_languages: list[str] = []
    for market_id in target_markets:
        for language in languages_by_market.get(market_id, []):
            if language not in all_languages:
                all_languages.append(language)
    target_locations = [detail["location_id"] for detail in recognized]
    regional_source_ids: list[str] = []
    for market_id in target_markets:
        for source_id in market_by_id[market_id]["source_ids"]:
            if source_id not in regional_source_ids:
                regional_source_ids.append(source_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "target_markets": target_markets,
        "target_roles": roles,
        "roles_source": roles_source,
        "generalized_roles": generalized_roles,
        "target_locations": target_locations,
        "location_details": details,
        "location_source": location_source,
        "report_language": report_language,
        "search_languages": all_languages,
        "search_languages_by_market": languages_by_market,
        "regional_source_ids": regional_source_ids,
        "search_plan": search_plan,
        "role_plan": role_plan,
        "max_websearch_calls": budget,
        "needs_user_input": needs_user_input,
        "warnings": warnings,
        "compatibility": {
            "multi_region_enabled": multi_region_enabled,
            "legacy_web_search_unchanged": True,
            "regional_sources_enabled": False,
        },
    }


def build_effective_profile(
    request: dict[str, Any],
    *,
    markets: dict[str, Any] | None = None,
    taxonomy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the CV profile the round's prefilter must actually be given.

    `user_intent.roles` reached the searches and nothing else: `build_market_plan`
    honours it, `job_prefilter` reads `roles` / `preferred_roles` off the profile
    file and never sees a user intent at all. So a round asked to look for
    Software Engineer went and looked, and then dropped every result on `role`.
    The plan resolves this once; this is that answer written where the filter
    reads it, and it is deliberately the same call, not a second copy of the
    precedence rule.

    Locations are left alone. The plan's are normalized ids scoped to a market,
    and `discovery_batch.py` already rejects an out-of-market candidate with its
    own `market` reason; writing "Dublin" into `locations` would additionally
    drop Cork, which nobody asked for.
    """
    plan = build_market_plan(request, markets=markets, taxonomy=taxonomy)
    if plan["needs_user_input"]:
        raise MarketPlanError(
            "the plan still needs user input, so there is no effective profile yet: "
            + "; ".join(plan["warnings"])
        )
    cv_profile = request.get("cv_profile") or {}
    # The generalizations belong here for the same reason the intent does: a
    # round that searches for Backend Engineer and then filters on the CV's
    # "AI Engineer" alone drops every generalized result on `role`, which is
    # the searches paying for a widening the filter never heard about.
    return {
        **cv_profile,
        "roles": _dedupe_strings([*plan["target_roles"], *plan["generalized_roles"]]),
    }


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "plan", "effective-profile"))
    parser.add_argument("--markets", type=Path, default=MARKETS_PATH)
    parser.add_argument("--roles", type=Path, default=ROLE_TAXONOMY_PATH)
    args = parser.parse_args()
    try:
        markets, taxonomy = load_resources(args.markets, args.roles)
        if args.command == "validate":
            _emit({
                "ok": True,
                "schema_version": SCHEMA_VERSION,
                "markets": len(markets["markets"]),
                "role_families": len(taxonomy["role_families"]),
            })
            return 0
        request = json.loads(read_stdin_text() or "{}")
        if args.command == "effective-profile":
            _emit({"ok": True, "profile": build_effective_profile(
                request, markets=markets, taxonomy=taxonomy
            )})
            return 0
        _emit({"ok": True, "plan": build_market_plan(
            request, markets=markets, taxonomy=taxonomy
        )})
        return 0
    except (MarketPlanError, StdinUnavailable, json.JSONDecodeError, TypeError, ValueError) as error:
        _emit({"ok": False, "error": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
