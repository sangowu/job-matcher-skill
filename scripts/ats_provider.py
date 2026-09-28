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
from typing import Any, Callable, NamedTuple, Protocol
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
ATS_PROVIDERS = ("ashby", "greenhouse", "lever", "workday")
PROVIDERS = ("amazon_jobs", *ATS_PROVIDERS)
# Providers whose listing can be read without the job descriptions, so the
# descriptions can be fetched per posting after the round has decided which
# postings it wants. Ashby, Lever and `amazon.jobs` embed the description in
# the only listing they serve and offer no way to ask for less.
#
# Greenhouse and Workday are here for opposite reasons. Greenhouse *can* leave
# them out: `?content=true` is a flag. Workday cannot include them: its listing
# has no description field at any setting, so a Workday posting has no
# description until `fetch_job_content` fetches one. Both are read the same way
# by the round, which is why they share the set.
CONTENT_DEFERRABLE_PROVIDERS = frozenset({"greenhouse", "workday"})
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
# Workday identifies a board by three things, all of them in the URL a person
# would open: the tenant, the data centre it is hosted in, and the career site.
# None can be dropped or derived -- measured 2026-09-28: no site segment answers
# 400, the wrong data centre answers 422, and the bare `<tenant>.myworkdayjobs.com`
# does not resolve.
_WORKDAY_DATA_CENTRE = re.compile(r"wd[0-9]{1,3}\Z")
_WORKDAY_SITE = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
# The listing gives a posting's path rather than an id, and that path is what
# the detail endpoint takes. It goes into a URL, so it is checked before it gets
# there rather than trusted because the listing it came from was ours.
_WORKDAY_PATH = re.compile(r"/job/[A-Za-z0-9._~%!$&'()*+,;=:@/-]{1,300}\Z")
# The endpoint refuses a larger page: 20 is 200, 50 is 400 (measured).
WORKDAY_PAGE_SIZE = 20
# A posting id goes into a URL path, so it is checked before it gets there
# rather than trusted because the listing it came from was ours.
_BOARD_JOB_ID = re.compile(r"[0-9]{1,32}\Z")
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
    def fetch_json(
        self, url: str, timeout_seconds: float, *, json_body: Any = None
    ) -> tuple[Any, int, float]: ...


class HttpAtsProvider:
    """Production transport: a public HTTPS read, with a bounded response.

    `json_body` makes the request a POST carrying that JSON. This was GET only,
    and the restriction was worth something: a GET cannot be mistaken for a
    write. Workday's search endpoint refuses GET outright -- 400 on the bare
    path and on every query-string spelling of the same arguments, measured
    2026-09-28 -- so reaching it at all means posting the search. What is posted
    is built in this module from a page offset and a limit; no caller supplies
    it, nothing from a CV or a profile goes into it, and the response is a
    listing. The verb is the endpoint's requirement, not a change of intent.
    """

    def __init__(self, *, accept_compression: bool = True) -> None:
        self.accept_compression = accept_compression

    def fetch_json(
        self, url: str, timeout_seconds: float, *, json_body: Any = None
    ) -> tuple[Any, int, float]:
        headers = {
            "Accept": "application/json",
            "User-Agent": (
                "JobMatcher-ATS/1.0 (+https://github.com/sangowu/job-matcher-skill)"
            ),
        }
        if self.accept_compression:
            headers["Accept-Encoding"] = "gzip"
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            url,
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
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
        self.bodies: list[Any] = []
        self._lock = threading.Lock()

    def fetch_json(
        self, url: str, _timeout_seconds: float, *, json_body: Any = None
    ) -> tuple[Any, int, float]:
        with self._lock:
            self.calls.append(url)
            self.bodies.append(json_body)
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
    if provider == "workday":
        # Checked here so a board missing one of the three fails before any
        # request rather than as a 400 from the other end.
        workday_identity(board)
    return provider, company, token


def workday_identity(board: dict[str, Any]) -> tuple[str, str, str]:
    """`(tenant, data_centre, site)`, or a refusal if any of them is unusable."""
    tenant = str(board.get("board_token", "")).strip()
    data_centre = str(board.get("instance", "")).strip().lower()
    site = str(board.get("site", "")).strip()
    if (
        not _BOARD_TOKEN.fullmatch(tenant)
        or not _WORKDAY_DATA_CENTRE.fullmatch(data_centre)
        or not _WORKDAY_SITE.fullmatch(site)
    ):
        raise AtsProviderError("invalid_board_token")
    return tenant, data_centre, site


def greenhouse_url(token: str, *, include_content: bool = True) -> str:
    suffix = "?content=true" if include_content else ""
    return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs{suffix}"


def greenhouse_job_url(token: str, job_id: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"


def workday_host(tenant: str, data_centre: str) -> str:
    return f"https://{tenant}.{data_centre}.myworkdayjobs.com"


def workday_url(tenant: str, data_centre: str, site: str) -> str:
    return f"{workday_host(tenant, data_centre)}/wday/cxs/{tenant}/{site}/jobs"


def workday_body(*, offset: int, limit: int) -> dict[str, Any]:
    """The search this module posts: one page, no search terms.

    `searchText` is empty and `appliedFacets` is empty on purpose. A board is
    fetched whole and filtered locally, which is what makes the result
    reproducible; sending terms here would give this one source a different and
    unrepeatable basis from every other, the same reason `amazon_jobs_url` takes
    no query.
    """
    return {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}


def workday_job_url(tenant: str, data_centre: str, site: str, path: str) -> str:
    """The detail endpoint, which is the only place a description exists."""
    return f"{workday_host(tenant, data_centre)}/wday/cxs/{tenant}/{site}{path}"


def workday_apply_url(tenant: str, data_centre: str, site: str, path: str) -> str:
    """The page a person opens, which is what a candidate row should link to."""
    return f"{workday_host(tenant, data_centre)}/{site}{path}"


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


def workday_job(
    company: str,
    job: dict[str, Any],
    *,
    tenant: str,
    data_centre: str,
    site: str,
) -> dict[str, Any] | None:
    """One listing row. Rows without a path or a title are not postings.

    Measured 2026-09-28 on NVIDIA's board: one row in the first twenty carried
    `bulletFields` and nothing else. `_candidate` already refuses a row with no
    title, url or id, so such a row is counted as unlisted rather than dropped
    silently.

    `date_posted` is left empty although the row has `postedOn`, because that
    field holds text like "Posted 30+ Days Ago" -- a description of when, not a
    date, and turning it into one would be inventing precision.
    """
    path = str(job.get("externalPath") or "").strip()
    if not _WORKDAY_PATH.fullmatch(path) or ".." in path:
        return None
    return _candidate(
        provider="workday",
        # The path is the board's own identifier for the posting and the only
        # thing the detail endpoint takes; there is no numeric id in the row.
        provider_id=path,
        company=company,
        title=str(job.get("title") or "").strip(),
        location=str(job.get("locationsText") or "").strip(),
        url=workday_apply_url(tenant, data_centre, site, path),
        description="",
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
    defer_content: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch and normalize one board; failures are classified and contained.

    `defer_content` reads the listing without job descriptions where the
    provider allows it, leaving `jd_text` empty for `fetch_job_content` to
    fill in for the postings the round actually keeps. A board is fetched
    whole and filtered locally, so every description on it is paid for and
    almost none of it is used: measured 2026-09-26, the structured channel
    downloaded 12,561 descriptions and committed 54 candidates. One GitLab
    board is 364,941 bytes with the descriptions and 11,330 without, and the
    listing still carries the title, location, id and URL that the prefilter
    and the identity key need. A provider that cannot serve a listing without
    descriptions ignores the flag rather than failing, since the round would
    otherwise lose those sources to save bytes on the others.
    """
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
        "content_deferred": False,
        "jobs_with_jd": 0,
        "jd_text_truncated": 0,
    }
    normalized: list[dict[str, Any]] = []

    def fetch(url: str, body: Any = None) -> Any:
        if request_budget is not None:
            request_budget.reserve()
        metrics["requests"] += 1
        metrics["pages_requested"] += 1
        try:
            payload, size, _ = client.fetch_json(url, timeout_seconds, json_body=body)
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
            if defer_content:
                metrics["content_deferred"] = True
                payload = fetch(greenhouse_url(token, include_content=False))
            else:
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
        elif provider == "workday":
            metrics["pagination"] = "offset_limit"
            # Not a choice: the listing carries no descriptions at any setting,
            # so every Workday posting arrives without one and the round fills
            # the ones it keeps through `fetch_job_content`.
            metrics["content_deferred"] = True
            tenant, data_centre, site = workday_identity(board)
            url = workday_url(tenant, data_centre, site)
            page_limit = min(page_size, WORKDAY_PAGE_SIZE)
            total: int | None = None
            exhausted = False
            for page in range(max_pages):
                payload = fetch(
                    url, workday_body(offset=page * page_limit, limit=page_limit)
                )
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("jobPostings"), list
                ):
                    raise AtsProviderError("invalid_payload")
                rows = payload["jobPostings"]
                if page == 0 and isinstance(payload.get("total"), int):
                    total = payload["total"]
                raw_jobs.extend(rows)
                # `total` is what ends this loop, and a short page is only a
                # fallback. An offset past the end does not return an empty
                # page here -- it returns the first page again, identically
                # (measured 2026-09-28 on a board of 2,000: offset 2000 and
                # offset 4000 both gave offset 0's twenty rows). Stopping only
                # on a short page would collect the same rows once per
                # remaining page and call the duplicates new postings. `total`
                # is also only reported on the first page, and reads 0 after
                # it, which is why it is captured once.
                if (total is not None and len(raw_jobs) >= total) or len(rows) < page_limit:
                    exhausted = True
                    break
            metrics["truncated"] = not exhausted

            def converter(
                company_name: str,
                raw: dict[str, Any],
                _tenant: str = tenant,
                _data_centre: str = data_centre,
                _site: str = site,
            ) -> dict[str, Any] | None:
                return workday_job(
                    company_name, raw, tenant=_tenant, data_centre=_data_centre, site=_site
                )

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


class _DeferredJd(NamedTuple):
    """How one provider answers "the description for this posting"."""

    url: Callable[[str], str | None]
    description: Callable[[dict[str, Any]], Any]


def _deferred_jd_reader(
    provider: str, token: str, board: dict[str, Any]
) -> _DeferredJd:
    """The per-provider half of a deferred description fetch.

    Both providers here defer, and neither identifies a posting the same way.
    Greenhouse gives a numeric id and returns `content`; Workday gives the
    posting's own path and returns `jobPostingInfo.jobDescription`. A provider
    that does not defer never reaches this: `fetch_job_content` returns before
    calling it.
    """
    if provider == "workday":
        tenant, data_centre, site = workday_identity(board)

        def workday_jd_url(path: str) -> str | None:
            if not path or ".." in path or not _WORKDAY_PATH.fullmatch(path):
                return None
            return workday_job_url(tenant, data_centre, site, path)

        def workday_description(payload: dict[str, Any]) -> Any:
            info = payload.get("jobPostingInfo")
            return info.get("jobDescription") if isinstance(info, dict) else None

        return _DeferredJd(workday_jd_url, workday_description)

    def greenhouse_jd_url(job_id: str) -> str | None:
        if not job_id or not _BOARD_JOB_ID.fullmatch(job_id):
            return None
        return greenhouse_job_url(token, job_id)

    return _DeferredJd(greenhouse_jd_url, lambda payload: payload.get("content"))


def fetch_job_content(
    board: dict[str, Any],
    jobs: list[dict[str, Any]],
    *,
    provider_client: AtsProvider | None = None,
    timeout_seconds: float = 30,
    request_budget: RequestBudget | None = None,
) -> dict[str, Any]:
    """Fill in the descriptions a deferred listing left empty, in place.

    One request per posting, so this is only worth calling for postings the
    round has already decided to keep -- it is the other half of
    `defer_content`, and calling it for a whole board would spend more
    requests than the single listing it replaced.

    A posting whose description cannot be fetched keeps its empty `jd_text`
    and is still returned: the evaluation worker's fallback ladder can read
    the page itself, which is slower but not a loss, while dropping the
    candidate would lose a job the round had already chosen over others. An
    exhausted request budget stops the pass instead of failing it, for the
    same reason.
    """
    started = time.perf_counter()
    client = provider_client or HttpAtsProvider()
    metrics: dict[str, Any] = {
        "action": "jd_fetch",
        "ok": False,
        "requests": 0,
        "response_bytes": 0,
        "jobs_requested": len(jobs),
        "jobs_filled": 0,
        "jobs_failed": 0,
        "jobs_skipped": 0,
        "jd_text_truncated": 0,
        "rate_limited": False,
        "failure_kind": "",
    }
    try:
        provider, _company, token = validate_board(board)
        jd_request = _deferred_jd_reader(provider, token, board)
    except AtsProviderError as error:
        metrics["failure_kind"] = error.kind
        metrics["jobs_skipped"] = len(jobs)
        metrics["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return metrics
    if provider not in CONTENT_DEFERRABLE_PROVIDERS:
        # Not an error: the caller asks for every kept posting and only some
        # boards deferred anything. These already hold their descriptions.
        metrics["ok"] = True
        metrics["jobs_skipped"] = len(jobs)
        metrics["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return metrics

    metrics["ok"] = True
    for index, job in enumerate(jobs):
        url = jd_request.url(str(job.get("provider_job_id") or "").strip())
        if url is None:
            metrics["jobs_skipped"] += 1
            continue
        try:
            if request_budget is not None:
                request_budget.reserve()
            metrics["requests"] += 1
            payload, size, _ = client.fetch_json(url, timeout_seconds)
            metrics["response_bytes"] += size
        except AtsProviderError as error:
            metrics["response_bytes"] += error.response_bytes
            if not metrics["failure_kind"]:
                metrics["failure_kind"] = error.kind
            if error.http_status == 429:
                metrics["rate_limited"] = True
            if error.kind == "request_budget_exhausted":
                metrics["jobs_skipped"] += len(jobs) - index
                break
            metrics["jobs_failed"] += 1
            continue
        if not isinstance(payload, dict):
            metrics["jobs_failed"] += 1
            if not metrics["failure_kind"]:
                metrics["failure_kind"] = "invalid_payload"
            continue
        jd_text, truncated = _normalize_jd_text(
            jd_request.description(payload), is_html=True
        )
        if not jd_text:
            metrics["jobs_failed"] += 1
            continue
        job["jd_text"] = jd_text
        job["jd_text_truncated"] = truncated
        job["description_present"] = True
        metrics["jobs_filled"] += 1
        metrics["jd_text_truncated"] += int(truncated)
    metrics["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return metrics
