#!/usr/bin/env python3
"""Run a bounded, count-only multi-region public-source smoke check.

This is an explicit diagnostic command, not a default discovery path. Raw page
content, job titles, company names, URLs, and job descriptions are kept only in
memory and are never written to the result artifact.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import ipaddress
import json
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

from _stdio import use_utf8_stdout


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = REPO_ROOT / "references" / "multi_region_smoke_plan.json"
DEFAULT_SEEDS = REPO_ROOT / "references" / "source_seeds.json"
SUPPORTED_MARKETS = ("ie", "uk", "cn", "de")
ALLOWED_MODES = {"public_get", "policy_skip"}
JOB_LINK_MARKERS = (
    "job",
    "jobs",
    "vacanc",
    "career",
    "position",
    "stelle",
    "recruit",
)
CAPTCHA_MARKERS = (
    "captcha",
    "recaptcha",
    "verify you are human",
    "unusual traffic",
    "人机验证",
    "安全验证",
)
APPLY_MARKERS = (
    "apply",
    "application",
    "bewerben",
    "jetzt bewerben",
    "申请",
)
JD_MARKERS = (
    "responsibilit",
    "requirement",
    "qualification",
    "experience",
    "aufgaben",
    "anforderungen",
    "任职要求",
    "岗位职责",
)


class SmokeError(RuntimeError):
    """Base error for invalid smoke configuration or execution."""


class TransportError(SmokeError):
    """A bounded public fetch failed for an external reason."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class FetchResult:
    status_code: int
    final_url: str
    body: str
    response_bytes: int
    duration_ms: int
    content_type: str


class SmokeTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        timeout_seconds: int,
        max_response_bytes: int,
        max_redirects: int,
    ) -> FetchResult: ...


def _normalized_host(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    return host.removeprefix("www.")


def _is_same_site(left: str, right: str) -> bool:
    left_host = _normalized_host(left)
    right_host = _normalized_host(right)
    if not left_host or not right_host:
        return False
    return (
        left_host == right_host
        or left_host.endswith(f".{right_host}")
        or right_host.endswith(f".{left_host}")
    )


def _validate_public_https(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise TransportError("unsafe_url", "only credential-free HTTPS URLs are allowed")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise TransportError("unsafe_url", "local hosts are not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return
    raise TransportError("unsafe_url", "literal IP addresses are not allowed")


class _LimitedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin_url: str, max_redirects: int) -> None:
        super().__init__()
        self.origin_url = origin_url
        self.max_redirects = max_redirects
        self.redirect_count = 0

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        self.redirect_count += 1
        if self.redirect_count > self.max_redirects:
            raise TransportError("redirect_limit", "redirect limit exceeded")
        absolute_url = urllib.parse.urljoin(req.full_url, newurl)
        _validate_public_https(absolute_url)
        if not _is_same_site(self.origin_url, absolute_url):
            raise TransportError("unsafe_redirect", "cross-site redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, absolute_url)


class BoundedHttpTransport:
    """Small stdlib HTTP client with fixed URL and response safety limits."""

    def get(
        self,
        url: str,
        *,
        timeout_seconds: int,
        max_response_bytes: int,
        max_redirects: int,
    ) -> FetchResult:
        _validate_public_https(url)
        started = time.monotonic()
        redirect_handler = _LimitedRedirectHandler(url, max_redirects)
        opener = urllib.request.build_opener(redirect_handler)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,text/plain;q=0.9",
                "Accept-Encoding": "gzip",
                "User-Agent": "JobMatcher-MultiRegion-Smoke/1.0",
            },
        )
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                status_code = int(response.status)
                final_url = response.geturl()
                content_type = response.headers.get_content_type().lower()
                content_encoding = response.headers.get("Content-Encoding", "").lower()
                raw = response.read(max_response_bytes + 1)
        except TransportError:
            raise
        except urllib.error.HTTPError as exc:
            raise TransportError("http_error", f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise TransportError("network_error", str(exc.reason)) from exc
        except TimeoutError as exc:
            raise TransportError("timeout", "request timed out") from exc
        except OSError as exc:
            raise TransportError("network_error", str(exc)) from exc

        if len(raw) > max_response_bytes:
            raise TransportError("response_limit", "response body exceeded byte limit")
        if content_type not in {"text/html", "text/plain"}:
            raise TransportError("content_type", "response was not HTML or plain text")
        if content_encoding == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError as exc:
                raise TransportError("invalid_gzip", "invalid gzip response") from exc
            if len(raw) > max_response_bytes:
                raise TransportError("response_limit", "decoded body exceeded byte limit")

        _validate_public_https(final_url)
        if not _is_same_site(url, final_url):
            raise TransportError("unsafe_redirect", "cross-site final URL refused")
        charset = "utf-8"
        try:
            charset = response.headers.get_content_charset() or "utf-8"
        except LookupError:
            pass
        body = raw.decode(charset, errors="replace")
        return FetchResult(
            status_code=status_code,
            final_url=final_url,
            body=body,
            response_bytes=len(raw),
            duration_ms=round((time.monotonic() - started) * 1000),
            content_type=content_type,
        )


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.visible_parts: list[str] = []
        self.title_parts: list[str] = []
        self.list_items: list[tuple[list[tuple[str, str]], str]] = []
        self._list_stack: list[dict[str, Any]] = []
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []
        self._hidden_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "li":
            self._list_stack.append({"links": [], "parts": []})
        if tag in {"script", "style", "noscript"}:
            self._hidden_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            self._anchor_href = dict(attrs).get("href")
            self._anchor_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor_href:
            link = (self._anchor_href, " ".join(self._anchor_parts))
            self.links.append(link)
            if self._list_stack:
                self._list_stack[-1]["links"].append(link)
            self._anchor_href = None
            self._anchor_parts = []
        if tag == "li" and self._list_stack:
            item = self._list_stack.pop()
            self.list_items.append((item["links"], " ".join(item["parts"])))
        if tag == "title":
            self._in_title = False
        if tag in {"script", "style", "noscript"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._hidden_depth:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        self.visible_parts.append(cleaned)
        if self._list_stack:
            self._list_stack[-1]["parts"].append(cleaned)
        if self._anchor_href is not None:
            self._anchor_parts.append(cleaned)
        if self._in_title:
            self.title_parts.append(cleaned)


def _parse_page(body: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(body)
    parser.close()
    return parser


def _normalized_text(value: str) -> str:
    return " ".join(urllib.parse.unquote(value).casefold().split())


def _contains_term(value: str, term: str) -> bool:
    if term.isascii() and len(term) <= 3 and term.isalnum():
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", value) is not None
    return term in value


def _blocked_reason(result: FetchResult, page: _PageParser) -> str | None:
    text = _normalized_text(result.body[:131072])
    if any(marker in text for marker in CAPTCHA_MARKERS):
        return "captcha_or_human_verification"
    path = urllib.parse.urlsplit(result.final_url).path.casefold()
    title = _normalized_text(" ".join(page.title_parts))
    if any(marker in path for marker in ("/login", "/signin", "/sign-in")):
        return "login_required"
    if title in {"login", "sign in", "log in", "登录"}:
        return "login_required"
    return None


def _canonical_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
    return urllib.parse.urlunsplit(("https", parsed.netloc.lower(), path, "", ""))


def _collect_candidates(
    page: _PageParser,
    base_url: str,
    role_terms: list[str],
    location_terms: list[str],
) -> tuple[list[str], dict[str, int]]:
    base_canonical = _canonical_url(base_url)
    card_text: dict[str, str] = {}
    for links, text in page.list_items:
        job_links = [href for href, _ in links if any(
            marker in _normalized_text(urllib.parse.urlsplit(href).path)
            for marker in JOB_LINK_MARKERS
        )]
        if len(job_links) == 1:
            absolute = urllib.parse.urljoin(base_url, job_links[0])
            if _is_same_site(base_url, absolute):
                card_text[_canonical_url(absolute)] = _normalized_text(text)
    raw: dict[str, tuple[str, str]] = {}
    for href, anchor_text in page.links:
        absolute = urllib.parse.urljoin(base_url, href)
        try:
            _validate_public_https(absolute)
        except TransportError:
            continue
        if not _is_same_site(base_url, absolute):
            continue
        canonical = _canonical_url(absolute)
        link_text = _normalized_text(f"{anchor_text} {urllib.parse.urlsplit(absolute).path}")
        if canonical == base_canonical or not any(marker in link_text for marker in JOB_LINK_MARKERS):
            continue
        raw.setdefault(canonical, (link_text, card_text.get(canonical, link_text)))

    normalized_roles = [_normalized_text(term) for term in role_terms]
    normalized_locations = [_normalized_text(term) for term in location_terms]
    role_matches = {
        url: texts
        for url, texts in raw.items()
        if any(_contains_term(texts[0], term) for term in normalized_roles)
    }
    location_matches = [
        url
        for url, texts in role_matches.items()
        if any(_contains_term(texts[1], term) for term in normalized_locations)
    ]
    return location_matches, {
        "candidates_raw": len(raw),
        "role_matched": len(role_matches),
        "location_matched": len(location_matches),
        "candidates_unique": len(location_matches),
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokeError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SmokeError(f"{path} must contain a JSON object")
    return value


def _positive_int(value: Any, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise SmokeError(f"{name} must be an integer between 1 and {maximum}")
    return value


def validate_plan(plan: dict[str, Any], seeds: dict[str, Any]) -> list[dict[str, Any]]:
    if plan.get("schema_version") != 1:
        raise SmokeError("smoke plan schema_version must be 1")
    limits = plan.get("limits")
    markets = plan.get("markets")
    if not isinstance(limits, dict) or not isinstance(markets, list):
        raise SmokeError("smoke plan must define limits and markets")
    _positive_int(limits.get("max_sources"), "max_sources", 8)
    _positive_int(limits.get("max_requests_per_source"), "max_requests_per_source", 3)
    _positive_int(limits.get("max_response_bytes"), "max_response_bytes", 1_048_576)
    _positive_int(limits.get("timeout_seconds"), "timeout_seconds", 20)
    _positive_int(limits.get("max_redirects"), "max_redirects", 3)
    if len(markets) != len(SUPPORTED_MARKETS) or len(markets) > limits["max_sources"]:
        raise SmokeError("smoke plan must contain exactly the four supported markets")

    source_rows = seeds.get("sources")
    if not isinstance(source_rows, list):
        raise SmokeError("source seed registry must contain a sources list")
    source_map = {
        source.get("source_id"): source
        for source in source_rows
        if isinstance(source, dict) and isinstance(source.get("source_id"), str)
    }
    seen_markets: set[str] = set()
    seen_slots: set[str] = set()
    resolved: list[dict[str, Any]] = []
    for item in markets:
        if not isinstance(item, dict):
            raise SmokeError("each market plan must be an object")
        market_id = item.get("market_id")
        slot = item.get("source_slot")
        source_id = item.get("source_id")
        mode = item.get("mode")
        if market_id not in SUPPORTED_MARKETS or market_id in seen_markets:
            raise SmokeError("market ids must be unique and limited to ie, uk, cn, de")
        if not isinstance(slot, str) or not re.fullmatch(r"[a-z]{2}-[a-z]+-\d+", slot):
            raise SmokeError("source_slot must be a stable opaque identifier")
        if slot in seen_slots:
            raise SmokeError("source_slot values must be unique")
        if mode not in ALLOWED_MODES:
            raise SmokeError("mode must be public_get or policy_skip")
        source = source_map.get(source_id)
        if source is None or market_id not in source.get("markets", []):
            raise SmokeError("each plan source must exist and cover its market")
        role_terms = item.get("role_terms")
        location_terms = item.get("location_terms")
        if not all(
            isinstance(values, list)
            and values
            and all(isinstance(term, str) and term.strip() for term in values)
            for values in (role_terms, location_terms)
        ):
            raise SmokeError("each market requires non-empty role_terms and location_terms")
        if mode == "public_get":
            if source.get("automation_allowed") is not True:
                raise SmokeError("public_get requires automation_allowed=true")
            if "public_read_only_page" not in source.get("access_methods", []):
                raise SmokeError("public_get requires public_read_only_page access")
            request_path = item.get("request_path")
            if request_path is not None:
                if not isinstance(request_path, str) or not request_path.startswith("/"):
                    raise SmokeError("request_path must be a same-site absolute path")
                parsed_path = urllib.parse.urlsplit(request_path)
                if parsed_path.scheme or parsed_path.netloc or parsed_path.fragment:
                    raise SmokeError("request_path must be a same-site absolute path")
                request_url = urllib.parse.urljoin(source["entry_url"], request_path)
                try:
                    _validate_public_https(request_url)
                except TransportError as exc:
                    raise SmokeError("request_path must resolve to public HTTPS") from exc
                if not _is_same_site(source["entry_url"], request_url):
                    raise SmokeError("request_path must stay on the source site")
        elif source.get("automation_allowed") is not False:
            raise SmokeError("policy_skip is reserved for automation-disallowed sources")
        elif "request_path" in item:
            raise SmokeError("policy_skip cannot define request_path")
        seen_markets.add(market_id)
        seen_slots.add(slot)
        resolved.append({**item, "source": source})
    if seen_markets != set(SUPPORTED_MARKETS):
        raise SmokeError("all four supported markets must be present")
    return resolved


def _empty_counts() -> dict[str, int | float | None]:
    return {
        "candidates_raw": 0,
        "role_matched": 0,
        "location_matched": 0,
        "candidates_unique": 0,
        "links_checked": 0,
        "live_verified": 0,
        "jd_checked": 0,
        "jd_available": 0,
        "application_route_visible": 0,
        "link_validity_rate": None,
        "jd_coverage_rate": None,
    }


def _run_source(
    item: dict[str, Any],
    limits: dict[str, int],
    transport: SmokeTransport,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "market_id": item["market_id"],
        "source_slot": item["source_slot"],
        "source_type": item["source"]["source_type"],
        "status": "inconclusive",
        "reason": None,
        "requests_made": 0,
        "response_bytes": 0,
        "duration_ms": 0,
        **_empty_counts(),
    }
    if item["mode"] == "policy_skip":
        record["status"] = "skipped_policy"
        record["reason"] = "source_policy_disallows_automation"
        return record

    def fetch(url: str) -> FetchResult:
        if record["requests_made"] >= limits["max_requests_per_source"]:
            raise TransportError("request_limit", "per-source request limit reached")
        record["requests_made"] += 1
        fetch_started = time.monotonic()
        try:
            result = transport.get(
                url,
                timeout_seconds=limits["timeout_seconds"],
                max_response_bytes=limits["max_response_bytes"],
                max_redirects=limits["max_redirects"],
            )
        finally:
            record["duration_ms"] += round((time.monotonic() - fetch_started) * 1000)
        record["response_bytes"] += result.response_bytes
        return result

    try:
        listing_url = urllib.parse.urljoin(
            item["source"]["entry_url"], item.get("request_path", "")
        )
        listing = fetch(listing_url)
        listing_page = _parse_page(listing.body)
        blocked = _blocked_reason(listing, listing_page)
        if blocked:
            record["status"] = "blocked"
            record["reason"] = blocked
            return record
        candidates, funnel = _collect_candidates(
            listing_page,
            listing.final_url,
            item["role_terms"],
            item["location_terms"],
        )
        record.update(funnel)
        if not candidates:
            record["status"] = "observed"
            record["reason"] = "no_matching_candidate_observed"
            return record

        detail = fetch(candidates[0])
        detail_page = _parse_page(detail.body)
        blocked = _blocked_reason(detail, detail_page)
        record["links_checked"] = 1
        record["jd_checked"] = 1
        if blocked:
            record["status"] = "blocked"
            record["reason"] = blocked
            record["link_validity_rate"] = 0.0
            record["jd_coverage_rate"] = 0.0
            return record
        record["live_verified"] = 1
        visible_text = _normalized_text(" ".join(detail_page.visible_parts))
        if len(visible_text) >= 500 and any(marker in visible_text for marker in JD_MARKERS):
            record["jd_available"] = 1
        for href, text in detail_page.links:
            signal = _normalized_text(f"{href} {text}")
            if any(marker in signal for marker in APPLY_MARKERS):
                record["application_route_visible"] = 1
                break
        if re.search(r"职位投递邮箱\s*[:：]\s*[^\s@]+@[^\s@]+", visible_text):
            record["application_route_visible"] = 1
        record["link_validity_rate"] = _ratio(
            record["live_verified"], record["links_checked"]
        )
        record["jd_coverage_rate"] = _ratio(record["jd_available"], record["jd_checked"])
        record["status"] = "observed"
        record["reason"] = None
        return record
    except TransportError as exc:
        record["status"] = "external_failure"
        record["reason"] = exc.kind
        return record


def run_smoke(
    plan: dict[str, Any],
    seeds: dict[str, Any],
    transport: SmokeTransport,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    resolved = validate_plan(plan, seeds)
    limits = plan["limits"]
    started = time.monotonic()
    source_results = [_run_source(item, limits, transport) for item in resolved]
    market_results = []
    for result in source_results:
        evidence_conclusion = "inconclusive"
        if result["status"] == "observed":
            evidence_conclusion = "preliminary"
            if (
                result["links_checked"] > 0
                and result["live_verified"] == result["links_checked"]
                and result["jd_checked"] > 0
                and result["jd_available"] > 0
            ):
                evidence_conclusion = "sufficient"
        market_results.append(
            {
                "market_id": result["market_id"],
                "coverage_status": result["status"],
                "source_success_rate": 1.0 if result["status"] == "observed" else 0.0,
                "candidates_raw": result["candidates_raw"],
                "candidates_unique": result["candidates_unique"],
                "links_checked": result["links_checked"],
                "live_verified": result["live_verified"],
                "jd_checked": result["jd_checked"],
                "jd_available": result["jd_available"],
                "evidence_conclusion": evidence_conclusion,
            }
        )
    plan_bytes = json.dumps(plan, sort_keys=True, ensure_ascii=False).encode("utf-8")
    timestamp = now or datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "run_kind": "explicit_bounded_live_smoke",
        "generated_at": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "limits": limits,
        "evidence_boundary": plan["evidence_boundary"],
        "privacy": {
            "count_only": True,
            "stores_company_names": False,
            "stores_job_titles": False,
            "stores_urls": False,
            "stores_job_descriptions": False,
        },
        "summary": {
            "markets_planned": len(source_results),
            "sources_observed": sum(row["status"] == "observed" for row in source_results),
            "sources_blocked": sum(row["status"] == "blocked" for row in source_results),
            "sources_external_failure": sum(
                row["status"] == "external_failure" for row in source_results
            ),
            "sources_skipped_policy": sum(
                row["status"] == "skipped_policy" for row in source_results
            ),
            "requests_made": sum(row["requests_made"] for row in source_results),
            "duration_ms": round((time.monotonic() - started) * 1000),
        },
        "markets": market_results,
        "sources": source_results,
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, newline="\n"
    ) as handle:
        handle.write(payload)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def main(argv: list[str] | None = None) -> int:
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="confirm an explicit live smoke run")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--source-seeds", type=Path, default=DEFAULT_SEEDS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("--live is required; this command must never run implicitly")
    try:
        report = run_smoke(
            _load_json(args.plan),
            _load_json(args.source_seeds),
            BoundedHttpTransport(),
        )
    except SmokeError as exc:
        parser.error(str(exc))
    if args.output:
        _write_json(args.output, report)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
