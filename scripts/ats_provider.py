#!/usr/bin/env python3
"""Bounded public GET adapters for Ashby, Greenhouse, and Lever job boards."""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from gzip import GzipFile
from html import unescape
from html.parser import HTMLParser
from io import BytesIO
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from _jobutil import canonicalize_url


MAX_RESPONSE_BYTES = 25 * 1024 * 1024
ATS_JD_MAX_CHARS = 50_000
# `amazon_jobs` is not an applicant tracking system: it is Amazon's own public
# job search endpoint, and it is here because this module is where a listing is
# fetched over HTTPS and normalized, which is the same job. Measured 2026-09-26:
# `search.json` filtered by country and asked for no query behaves exactly like
# a board -- 208 Irish postings, 32 fields each including the full description,
# offset pagination, `hits` giving the total. The alternative was clicking
# through the same postings in a browser at roughly five seconds a page.
# The three applicant tracking systems. `amazon_jobs` is fetched by the same
# machinery and is not one of them, and the distinction matters wherever a
# rule is about ATS boards rather than about anything this module can fetch.
ATS_PROVIDERS = ("ashby", "greenhouse", "lever")
PROVIDERS = ("amazon_jobs", *ATS_PROVIDERS)
AMAZON_JOBS_HOST = "https://www.amazon.jobs"
# ISO-3166 alpha-3, which is what the endpoint's country filter takes.
_AMAZON_COUNTRY = re.compile(r"[A-Z]{3}\Z")
_AMAZON_DATE = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})")
_MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ),
        start=1,
    )
}
_BOARD_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_SAFE_FAILURES = {
    "http_error",
    "network_error",
    "timeout",
    "invalid_json",
    "invalid_compression",
    "invalid_payload",
    "response_too_large",
    "unsupported_provider",
    "invalid_board_token",
    "request_budget_exhausted",
}


class _JobDescriptionParser(HTMLParser):
    """Small dependency-free HTML-to-text converter for untrusted ATS data."""

    _BLOCKS = {
        "address", "article", "aside", "blockquote", "br", "div", "dl", "dt",
        "dd", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li", "ol", "p",
        "pre", "section", "table", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _clean_jd_text(value: Any, *, is_html: bool) -> str:
    text = str(value or "")
    if is_html and text:
        # Greenhouse can return HTML whose tags are themselves entity-escaped.
        # Decode a bounded number of layers before parsing so those tags do not
        # leak into the evaluation text (and encoded script/style stays ignored).
        for _ in range(2):
            decoded = unescape(text)
            if decoded == text:
                break
            text = decoded
        parser = _JobDescriptionParser()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _normalize_jd_text(value: Any, *, is_html: bool) -> tuple[str, bool]:
    text = _clean_jd_text(value, is_html=is_html)
    truncated = len(text) > ATS_JD_MAX_CHARS
    return text[:ATS_JD_MAX_CHARS], truncated


class AtsProviderError(RuntimeError):
    def __init__(
        self,
        kind: str,
        http_status: int | None = None,
        response_bytes: int = 0,
    ) -> None:
        super().__init__(kind)
        self.kind = kind if kind in _SAFE_FAILURES else "network_error"
        self.http_status = http_status
        self.response_bytes = max(0, int(response_bytes))


class AtsProvider(Protocol):
    def fetch_json(self, url: str, timeout_seconds: float) -> tuple[Any, int, float]: ...


class HttpAtsProvider:
    """Production transport: public HTTPS GET only, with a bounded response."""

    def __init__(self, *, accept_compression: bool = True) -> None:
        self.accept_compression = accept_compression

    def fetch_json(self, url: str, timeout_seconds: float) -> tuple[Any, int, float]:
        headers = {
            "Accept": "application/json",
            "User-Agent": (
                "JobMatcher-ATS/1.0 (+https://github.com/sangowu/job-matcher-skill)"
            ),
        }
        if self.accept_compression:
            headers["Accept-Encoding"] = "gzip"
        request = Request(
            url,
            headers=headers,
            method="GET",
        )
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                content_encoding = str(
                    response.headers.get("Content-Encoding", "")
                ).strip().lower()
        except HTTPError as error:
            raise AtsProviderError("http_error", error.code) from error
        except TimeoutError as error:
            raise AtsProviderError("timeout") from error
        except (URLError, OSError) as error:
            raise AtsProviderError("network_error") from error
        duration_ms = (time.perf_counter() - started) * 1000
        response_bytes = len(payload)
        if response_bytes > MAX_RESPONSE_BYTES:
            raise AtsProviderError("response_too_large", response_bytes=response_bytes)
        if content_encoding == "gzip":
            try:
                with GzipFile(fileobj=BytesIO(payload)) as compressed:
                    payload = compressed.read(MAX_RESPONSE_BYTES + 1)
            except (EOFError, OSError) as error:
                raise AtsProviderError(
                    "invalid_compression", response_bytes=response_bytes
                ) from error
            if len(payload) > MAX_RESPONSE_BYTES:
                raise AtsProviderError(
                    "response_too_large", response_bytes=response_bytes
                )
        elif content_encoding not in {"", "identity"}:
            raise AtsProviderError(
                "invalid_compression", response_bytes=response_bytes
            )
        try:
            return json.loads(payload), response_bytes, duration_ms
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AtsProviderError("invalid_json") from error


class FakeAtsProvider:
    """Deterministic scripted transport for tests and offline benchmarks."""

    def __init__(self, responses: list[Any] | dict[str, list[Any]]) -> None:
        self._responses = deque(responses) if isinstance(responses, list) else None
        self._routes = {
            marker: deque(values) for marker, values in responses.items()
        } if isinstance(responses, dict) else {}
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def fetch_json(self, url: str, _timeout_seconds: float) -> tuple[Any, int, float]:
        with self._lock:
            self.calls.append(url)
            queue = self._responses
            if self._routes:
                queue = next((values for marker, values in self._routes.items() if marker in url), None)
            if not queue:
                raise AssertionError(f"FakeAtsProvider has no scripted response for {url}")
            response = queue.popleft()
        if isinstance(response, Exception):
            raise response
        if (
            isinstance(response, tuple)
            and len(response) == 3
            and isinstance(response[1], int)
        ):
            return response
        size = len(json.dumps(response, ensure_ascii=False).encode("utf-8"))
        return response, size, 1.0


class RequestBudget:
    """Thread-safe request admission shared by concurrent board fetches."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("request budget must be positive")
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def reserve(self) -> None:
        with self._lock:
            if self.used >= self.limit:
                raise AtsProviderError("request_budget_exhausted")
            self.used += 1


def validate_board(board: dict[str, Any]) -> tuple[str, str, str]:
    provider = str(board.get("provider", "")).lower()
    company = str(board.get("company", "")).strip()
    token = str(board.get("board_token", "")).strip()
    if provider not in PROVIDERS:
        raise AtsProviderError("unsupported_provider")
    if not company or not _BOARD_TOKEN.fullmatch(token):
        raise AtsProviderError("invalid_board_token")
    if provider == "amazon_jobs" and not _AMAZON_COUNTRY.fullmatch(token):
        # Here the token is which country's listing to read, so a board token
        # shaped like an ATS slug would silently fetch the global listing.
        raise AtsProviderError("invalid_board_token")
    return provider, company, token


def greenhouse_url(token: str, *, include_content: bool = True) -> str:
    suffix = "?content=true" if include_content else ""
    return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs{suffix}"


def ashby_url(token: str) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"


def amazon_jobs_url(country: str, *, offset: int, limit: int) -> str:
    """One page of Amazon's public job search, filtered only by country.

    No `base_query`. An ATS board is fetched whole and filtered locally, which
    is what makes its result reproducible and its query-writing unnecessary;
    sending search terms here would give this one source a different and
    unrepeatable basis from every other. The country filter is not a search
    term -- it is which listing is being read.
    """
    query = urlencode(
        {
            "normalized_country_code[]": country,
            "offset": offset,
            "result_limit": limit,
            "sort": "recent",
        }
    )
    return f"{AMAZON_JOBS_HOST}/search.json?{query}"


def _amazon_date(value: Any) -> str:
    """`September  9, 2026` -> `2026-09-09`; anything else -> empty.

    Every other provider hands over a machine-readable timestamp. This one
    hands over the string it prints on the page, so it is parsed here rather
    than passed downstream for something else to guess at.
    """
    match = _AMAZON_DATE.search(str(value or ""))
    if match is None:
        return ""
    month = _MONTHS.get(match.group(1).strip().lower())
    if month is None:
        return ""
    return f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(2)):02d}"


def amazon_jobs_job(company: str, job: dict[str, Any]) -> dict[str, Any] | None:
    path = str(job.get("job_path") or "").strip()
    # A posting is identified by the numeric id in its own URL. `id` is a uuid
    # that the URL does not contain, so it could not be matched back to a link
    # found anywhere else.
    provider_id = str(job.get("id_icims") or "").strip()
    description = "".join(
        f"<p>{section}</p>"
        for section in (
            str(job.get("description") or ""),
            str(job.get("basic_qualifications") or ""),
            str(job.get("preferred_qualifications") or ""),
        )
        if section.strip()
    )
    return _candidate(
        provider="amazon_jobs",
        provider_id=provider_id,
        # The board's name, not the posting's `company_name`: that field holds
        # the hiring legal entity ("Amazon Data Services Ireland Limited"), and
        # letting it through would split one employer into several dedup keys.
        company=company,
        title=str(job.get("title") or "").strip(),
        location=str(job.get("normalized_location") or job.get("location") or "").strip(),
        url=f"{AMAZON_JOBS_HOST}{path}" if path.startswith("/") else "",
        description=description,
        description_is_html=True,
        date_posted=_amazon_date(job.get("posted_date")),
    )


def lever_url(token: str, instance: str, *, skip: int, limit: int) -> str:
    host = "api.eu.lever.co" if instance == "eu" else "api.lever.co"
    query = urlencode({"mode": "json", "skip": skip, "limit": limit})
    return f"https://{host}/v0/postings/{token}?{query}"


def _candidate(
    *,
    provider: str,
    provider_id: str,
    company: str,
    title: str,
    location: str,
    url: str,
    description: Any = "",
    description_is_html: bool = False,
    date_posted: str = "",
    salary: str = "",
) -> dict[str, Any] | None:
    if not title or not url or not provider_id:
        return None
    jd_text, jd_text_truncated = _normalize_jd_text(
        description, is_html=description_is_html
    )
    return {
        "provider": provider,
        "provider_job_id": provider_id,
        "identity_keys": [f"{provider}:{provider_id}".lower()],
        "company": company,
        "title": title,
        "location": location,
        "url": url,
        "snippet": "",
        "salary": salary,
        "date_posted": date_posted,
        "source": provider,
        "description_present": bool(jd_text),
        "jd_text": jd_text,
        "jd_text_truncated": jd_text_truncated,
    }


def greenhouse_job(company: str, job: dict[str, Any]) -> dict[str, Any] | None:
    location_value = job.get("location")
    location = str(location_value.get("name") or "") if isinstance(location_value, dict) else ""
    return _candidate(
        provider="greenhouse",
        provider_id=str(job.get("id") or "").strip(),
        company=company,
        title=str(job.get("title") or "").strip(),
        location=location,
        url=str(job.get("absolute_url") or "").strip(),
        description=job.get("content"),
        description_is_html=True,
        date_posted=str(job.get("updated_at") or "").strip(),
    )


def ashby_job(company: str, job: dict[str, Any]) -> dict[str, Any] | None:
    if job.get("isListed") is False:
        return None
    url = str(job.get("jobUrl") or "").strip()
    provider_key = canonicalize_url(url)
    provider_id = provider_key.split(":", 1)[1] if provider_key.startswith("ashby:") else ""
    secondary = job.get("secondaryLocations")
    secondary_names = [
        str(item.get("location") or "").strip()
        for item in secondary or []
        if isinstance(item, dict) and item.get("location")
    ]
    locations = [str(job.get("location") or "").strip(), *secondary_names]
    compensation = job.get("compensation")
    salary = ""
    if isinstance(compensation, dict):
        salary = str(compensation.get("scrapeableCompensationSalarySummary") or "").strip()
    plain_description = job.get("descriptionPlain")
    return _candidate(
        provider="ashby",
        provider_id=provider_id,
        company=company,
        title=str(job.get("title") or "").strip(),
        location="; ".join(value for value in locations if value),
        url=url,
        description=plain_description or job.get("descriptionHtml"),
        description_is_html=not bool(plain_description),
        date_posted=str(job.get("publishedAt") or "").strip(),
        salary=salary,
    )


def lever_job(company: str, job: dict[str, Any]) -> dict[str, Any] | None:
    categories = job.get("categories")
    locations: list[str] = []
    if isinstance(categories, dict):
        primary = str(categories.get("location") or "").strip()
        if primary:
            locations.append(primary)
        for value in categories.get("allLocations") or []:
            text = str(value or "").strip()
            if text and text not in locations:
                locations.append(text)
    salary = str(job.get("salaryDescriptionPlain") or "").strip()
    plain_description = job.get("descriptionPlain")
    description_parts = [
        _clean_jd_text(
            plain_description or job.get("description"),
            is_html=not bool(plain_description),
        )
    ]
    lists = job.get("lists")
    if isinstance(lists, list):
        for item in lists:
            if not isinstance(item, dict):
                continue
            description_parts.extend((
                _clean_jd_text(item.get("text"), is_html=False),
                _clean_jd_text(item.get("content"), is_html=True),
            ))
    additional_plain = job.get("additionalPlain")
    description_parts.append(_clean_jd_text(
        additional_plain or job.get("additional"),
        is_html=not bool(additional_plain),
    ))
    return _candidate(
        provider="lever",
        provider_id=str(job.get("id") or "").strip(),
        company=company,
        title=str(job.get("text") or "").strip(),
        location="; ".join(locations),
        url=str(job.get("hostedUrl") or "").strip(),
        description="\n".join(part for part in description_parts if part),
        salary=salary,
    )


def fetch_board(
    board: dict[str, Any],
    *,
    provider_client: AtsProvider | None = None,
    page_size: int = 50,
    max_pages: int = 10,
    timeout_seconds: float = 30,
    request_budget: RequestBudget | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch and normalize one board; failures are classified and contained."""
    started = time.perf_counter()
    client = provider_client or HttpAtsProvider()
    provider = str(board.get("provider", "")).lower() or "unknown"
    company = str(board.get("company", "")).strip() or "unknown"
    token = str(board.get("board_token", "")).strip()
    metrics: dict[str, Any] = {
        "provider": provider,
        "company": company,
        "board_token": token,
        "ok": False,
        "pagination": "unknown",
        "requests": 0,
        "pages_requested": 0,
        "response_bytes": 0,
        "jobs_received": 0,
        "jobs_normalized": 0,
        "invalid_or_unlisted_jobs": 0,
        "truncated": False,
        "rate_limited": False,
        "content_fallback": False,
        "jobs_with_jd": 0,
        "jd_text_truncated": 0,
    }
    normalized: list[dict[str, Any]] = []

    def fetch(url: str) -> Any:
        if request_budget is not None:
            request_budget.reserve()
        metrics["requests"] += 1
        metrics["pages_requested"] += 1
        try:
            payload, size, _ = client.fetch_json(url, timeout_seconds)
        except AtsProviderError as error:
            metrics["response_bytes"] += error.response_bytes
            raise
        metrics["response_bytes"] += size
        return payload

    try:
        provider, company, token = validate_board(board)
        metrics.update(provider=provider, company=company, board_token=token)
        raw_jobs: list[Any] = []
        if provider == "greenhouse":
            metrics["pagination"] = "single_response"
            try:
                payload = fetch(greenhouse_url(token))
            except AtsProviderError as error:
                if error.kind != "response_too_large":
                    raise
                metrics["content_fallback"] = True
                payload = fetch(greenhouse_url(token, include_content=False))
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                raise AtsProviderError("invalid_payload")
            raw_jobs = payload["jobs"]
            converter = greenhouse_job
        elif provider == "ashby":
            metrics["pagination"] = "single_response"
            payload = fetch(ashby_url(token))
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                raise AtsProviderError("invalid_payload")
            raw_jobs = payload["jobs"]
            converter = ashby_job
        elif provider == "amazon_jobs":
            metrics["pagination"] = "offset_limit"
            exhausted = False
            for page in range(max_pages):
                payload = fetch(
                    amazon_jobs_url(token, offset=page * page_size, limit=page_size)
                )
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("jobs"), list
                ):
                    raise AtsProviderError("invalid_payload")
                raw_jobs.extend(payload["jobs"])
                # `hits` is the total the endpoint says it has. Trusting only
                # a short page would spend one extra request per board when the
                # last page happens to be full.
                hits = payload.get("hits")
                if len(payload["jobs"]) < page_size or (
                    isinstance(hits, int) and len(raw_jobs) >= hits
                ):
                    exhausted = True
                    break
            metrics["truncated"] = not exhausted
            converter = amazon_jobs_job
        else:
            metrics["pagination"] = "offset_limit"
            instance = str(board.get("instance", "global")).lower()
            if instance not in {"global", "eu"}:
                raise AtsProviderError("invalid_board_token")
            exhausted = False
            for page in range(max_pages):
                payload = fetch(lever_url(
                    token, instance, skip=page * page_size, limit=page_size
                ))
                if not isinstance(payload, list):
                    raise AtsProviderError("invalid_payload")
                raw_jobs.extend(payload)
                if len(payload) < page_size:
                    exhausted = True
                    break
            metrics["truncated"] = not exhausted
            converter = lever_job

        metrics["jobs_received"] = len(raw_jobs)
        for raw in raw_jobs:
            converted = converter(company, raw) if isinstance(raw, dict) else None
            if converted is None:
                metrics["invalid_or_unlisted_jobs"] += 1
            else:
                normalized.append(converted)
        metrics["jobs_normalized"] = len(normalized)
        metrics["jobs_with_jd"] = sum(bool(job.get("jd_text")) for job in normalized)
        metrics["jd_text_truncated"] = sum(
            bool(job.get("jd_text_truncated")) for job in normalized
        )
        metrics["ok"] = True
    except AtsProviderError as error:
        metrics["failure_kind"] = error.kind
        if error.http_status is not None:
            metrics["http_status"] = error.http_status
            metrics["rate_limited"] = error.http_status == 429
    metrics["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return metrics, normalized
