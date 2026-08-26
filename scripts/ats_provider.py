#!/usr/bin/env python3
"""Bounded public GET adapters for Ashby, Greenhouse, and Lever job boards."""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from _jobutil import canonicalize_url


MAX_RESPONSE_BYTES = 25 * 1024 * 1024
PROVIDERS = ("ashby", "greenhouse", "lever")
_BOARD_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_SAFE_FAILURES = {
    "http_error",
    "network_error",
    "timeout",
    "invalid_json",
    "invalid_payload",
    "response_too_large",
    "unsupported_provider",
    "invalid_board_token",
    "request_budget_exhausted",
}


class AtsProviderError(RuntimeError):
    def __init__(self, kind: str, http_status: int | None = None) -> None:
        super().__init__(kind)
        self.kind = kind if kind in _SAFE_FAILURES else "network_error"
        self.http_status = http_status


class AtsProvider(Protocol):
    def fetch_json(self, url: str, timeout_seconds: float) -> tuple[Any, int, float]: ...


class HttpAtsProvider:
    """Production transport: public HTTPS GET only, with a bounded response."""

    def fetch_json(self, url: str, timeout_seconds: float) -> tuple[Any, int, float]:
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "JobMatcher-ATS/1.0 (+https://github.com/sangowu/job-matcher-skill)"
                ),
            },
            method="GET",
        )
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raise AtsProviderError("http_error", error.code) from error
        except TimeoutError as error:
            raise AtsProviderError("timeout") from error
        except (URLError, OSError) as error:
            raise AtsProviderError("network_error") from error
        duration_ms = (time.perf_counter() - started) * 1000
        if len(payload) > MAX_RESPONSE_BYTES:
            raise AtsProviderError("response_too_large")
        try:
            return json.loads(payload), len(payload), duration_ms
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
    return provider, company, token


def greenhouse_url(token: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def ashby_url(token: str) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"


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
    description_present: bool,
    date_posted: str = "",
    salary: str = "",
) -> dict[str, Any] | None:
    if not title or not url or not provider_id:
        return None
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
        "description_present": description_present,
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
        description_present=bool(job.get("content")),
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
    return _candidate(
        provider="ashby",
        provider_id=provider_id,
        company=company,
        title=str(job.get("title") or "").strip(),
        location="; ".join(value for value in locations if value),
        url=url,
        description_present=bool(job.get("descriptionPlain") or job.get("descriptionHtml")),
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
    return _candidate(
        provider="lever",
        provider_id=str(job.get("id") or "").strip(),
        company=company,
        title=str(job.get("text") or "").strip(),
        location="; ".join(locations),
        url=str(job.get("hostedUrl") or "").strip(),
        description_present=bool(job.get("descriptionPlain") or job.get("description")),
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
    }
    normalized: list[dict[str, Any]] = []

    def fetch(url: str) -> Any:
        if request_budget is not None:
            request_budget.reserve()
        metrics["requests"] += 1
        metrics["pages_requested"] += 1
        payload, size, _ = client.fetch_json(url, timeout_seconds)
        metrics["response_bytes"] += size
        return payload

    try:
        provider, company, token = validate_board(board)
        metrics.update(provider=provider, company=company, board_token=token)
        raw_jobs: list[Any] = []
        if provider == "greenhouse":
            metrics["pagination"] = "single_response"
            payload = fetch(greenhouse_url(token))
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
        metrics["ok"] = True
    except AtsProviderError as error:
        metrics["failure_kind"] = error.kind
        if error.http_status is not None:
            metrics["http_status"] = error.http_status
            metrics["rate_limited"] = error.http_status == 429
    metrics["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return metrics, normalized
