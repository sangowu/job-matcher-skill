#!/usr/bin/env python3
"""Discover, verify, prefilter, and emit public ATS job-board candidates."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from _jobutil import load_config, normalize_company
from _stdio import StdinUnavailable, read_stdin_text, use_utf8_stdout
from ats_provider import (
    AtsProvider,
    HttpAtsProvider,
    RequestBudget,
    fetch_board,
    fetch_job_content,
)
# The prefilter is a rule about jobs, not about this channel; it lives in
# its own module now so the browser and Web Search channels are held to the
# same one. Re-exported here because these names are this module's API to
# its existing callers.
from job_prefilter import (  # noqa: F401
    _location_matches,
    _normalized_text,
    _role_families,
    _seniority_matches,
    _title_matches,
    prefilter_jobs,
    rejection_reason,
    level_tier,
)
from runtime_metrics import record_metric, validate_run_id
import market_plan
import source_registry


SKILL_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_ROOT / "data"
REGISTRY_PATH = DATA_DIR / "ats_companies.json"
SYNC_STATE_PATH = DATA_DIR / "ats_sync_state.json"
METRICS_PATH = DATA_DIR / "metrics.jsonl"
_UNAVAILABLE_STATUSES = {404, 410}
# A posting says "Remote - US" as readily as it says "Remote", and the label
# alone cannot tell the two apart -- half of them name the eligible countries,
# or even the eligible US states, only in the description. Rather than guess a
# jurisdiction, a round does not search remote work at all. See docs/roadmap.md.
# Not every structured source is an ATS board. `amazon-jobs-ie` is catalogued
# as `company_careers` reading a `public_read_only_endpoint`, and
# `discovery_batch.py` checks each candidate's `source_type` against the type
# its task carries from the catalog, so a hardcoded `ats_board` made every
# Amazon candidate mismatch its own task. One mismatch fails the whole wave:
# 2026-09-26 fetched 23 boards and 4,187 jobs and committed none of them.
# The caller supplies the catalogued type per source and an unmapped type is
# an error, because a default is what mislabelled this one in silence.
_STRUCTURED_ROUTES = {
    "ats_board": "ats_expansion",
    "company_careers": "company_careers",
}
class AtsPipelineError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_document(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AtsPipelineError(f"cannot read valid ATS state: {path.name}") from error
    if not isinstance(payload, dict):
        raise AtsPipelineError(f"ATS state must be an object: {path.name}")
    return payload


def _save_document(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = _now().isoformat()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _generic_registry_path() -> Path:
    return REGISTRY_PATH.with_name("source_registry.json")


def _load_ats_registry() -> dict[str, Any]:
    generic_path = _generic_registry_path()
    if not generic_path.exists():
        return _load_document(REGISTRY_PATH, {"schema_version": 1, "boards": []})
    try:
        generic = source_registry.load_registry(generic_path)
        marker = generic["migrations"].get("ats_companies_v1")
        if (
            isinstance(marker, dict)
            and marker.get("status") == "rolled_back"
            and REGISTRY_PATH.exists()
        ):
            return _load_document(REGISTRY_PATH, {"schema_version": 1, "boards": []})
        return source_registry.ats_view_from_registry(generic)
    except source_registry.SourceRegistryError as error:
        raise AtsPipelineError("cannot read generic source registry") from error


def _save_ats_registry(registry: dict[str, Any]) -> None:
    generic_path = _generic_registry_path()
    if not generic_path.exists():
        _save_document(REGISTRY_PATH, registry)
        return
    try:
        generic = source_registry.load_registry(generic_path)
        marker = generic["migrations"].get("ats_companies_v1")
        if isinstance(marker, dict) and marker.get("status") == "rolled_back":
            # Rollback exposes the legacy file as a read-only compatibility view.
            return
        source_registry.commit_ats_view(
            registry,
            registry_path=generic_path,
            lock_path=generic_path.with_name("source_registry.lock"),
        )
    except source_registry.SourceRegistryError as error:
        raise AtsPipelineError("cannot write generic source registry") from error


def _board_id(provider: str, token: str, instance: str) -> str:
    digest = hashlib.sha256(f"{provider}|{instance}|{token.lower()}".encode()).hexdigest()[:20]
    return f"ats_{digest}"


def extract_board_marker(url: str, company: str) -> dict[str, Any] | None:
    """Extract an allowlisted board marker from an observed public job URL."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or not company:
        return None
    provider = ""
    instance = "global"
    if host == "jobs.ashbyhq.com":
        provider = "ashby"
    elif host in {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }:
        provider = "greenhouse"
    elif host == "jobs.lever.co":
        provider = "lever"
    elif host == "jobs.eu.lever.co":
        provider = "lever"
        instance = "eu"
    else:
        return None
    token = parts[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", token):
        return None
    now = _now().isoformat()
    return {
        "board_id": _board_id(provider, token, instance),
        "company_key": normalize_company(company),
        "company": str(company).strip(),
        "provider": provider,
        "board_token": token,
        "instance": instance,
        "status": "candidate",
        "enabled": True,
        "first_seen_at": now,
        "last_seen_at": now,
        "last_attempt_at": None,
        "last_success_at": None,
        "consecutive_unavailable": 0,
    }


def discover_candidates(
    candidates: list[Any], registry: dict[str, Any]
) -> dict[str, int]:
    boards = registry.setdefault("boards", [])
    if not isinstance(boards, list):
        raise AtsPipelineError("ATS registry boards must be a list")
    by_id = {
        str(board.get("board_id")): board for board in boards if isinstance(board, dict)
    }
    discovered = 0
    existing = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        marker = extract_board_marker(candidate.get("url", ""), candidate.get("company", ""))
        if marker is None:
            continue
        current = by_id.get(marker["board_id"])
        if current is None:
            boards.append(marker)
            by_id[marker["board_id"]] = marker
            discovered += 1
        else:
            current["last_seen_at"] = marker["last_seen_at"]
            if not current.get("company"):
                current["company"] = marker["company"]
            existing += 1
    registry["schema_version"] = 1
    return {"discovered": discovered, "existing": existing, "registry_size": len(boards)}


def filter_to_markets(
    jobs: list[dict[str, Any]],
    markets: list[str],
    *,
    resources: dict[str, Any],
) -> list[dict[str, Any]]:
    """Keep only the jobs the market plan attributes to one of `markets`.

    A round is scoped to a market and the structured channel had no way to know
    it. `prefilter_jobs` reads locations off the CV profile, and this profile
    lists none -- `extract_cv.py` reports `target_locations` as missing -- so
    `_location_matches` read "no preferred locations" as "anywhere" and a global
    board's entire world passed. That spent the candidate cap on postings
    outside the round, and then aborted the whole wave as soon as one of them
    landed in another supported market, because `discovery_batch.py` checks each
    candidate's market against its task's (2026-09-26: 23 boards fetched, 4,187
    jobs, nothing committed).

    A job whose location resolves to no market at all is dropped too. "Seattle,
    WA" is not an Irish posting just because the catalog has no US market.
    """
    scoped = set(markets)
    kept = []
    for job in jobs:
        location = market_plan.normalize_location(job.get("location"), resources)
        if scoped & set(location["market_ids"]):
            kept.append(job)
    return kept


def _clean_candidate(job: dict[str, Any], board_id: str) -> dict[str, Any]:
    fields = (
        "title", "company", "location", "url", "snippet", "salary",
        "date_posted", "source", "identity_keys", "jd_text", "jd_text_truncated",
    )
    cleaned = {
        field: job.get(
            field,
            [] if field == "identity_keys" else False if field == "jd_text_truncated" else "",
        )
        for field in fields
    }
    # Which board answered is not recoverable from the job itself, and a caller
    # that has to attribute a candidate back to its planned task needs it.
    # `board_id` is already the registry's source id, spelled the same way.
    cleaned["source_id"] = board_id
    return cleaned



def to_candidate_envelopes(
    candidates: list[dict[str, Any]],
    *,
    source_types: dict[str, str],
    markets: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Turn synced ATS candidates into CandidateEnvelopes, JD text kept beside.

    This channel predates the CandidateEnvelope contract and emitted a shape
    that only `merge_jobs.py` would take, which is why its jobs could reach the
    table only through a second writer. The envelope forbids job description
    text outright, so the text travels next to the envelope instead of inside
    it: the contract stays intact and the text never has to pass through the
    orchestrator on its way to the merge.

    `source_types` maps each candidate's `source_id` to the `source_type` the
    public catalog gives that source. It has no default: the envelope must say
    which kind of source the posting actually came from, and the caller is the
    one holding the catalog.

    Returns one `(envelope, jd)` pair per candidate, in the order given.
    """
    resolved = markets if markets is not None else market_plan.load_resources()[0]
    observed_at = (now or _now()).isoformat().replace("+00:00", "Z")
    languages = {
        market["market_id"]: (market.get("default_search_languages") or ["en"])[0]
        for market in resolved["markets"]
    }
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for candidate in candidates:
        source_type = source_types.get(candidate.get("source_id", ""))
        route = _STRUCTURED_ROUTES.get(source_type or "")
        if route is None:
            raise AtsPipelineError(
                "structured candidate has no catalogued source_type: "
                f"{source_type or 'unmapped source_id'}"
            )
        location = market_plan.normalize_location(candidate.get("location"), resolved)
        market_ids = location["market_ids"]
        # Exactly one market is an attribution. Several is not one market, so
        # the singular field says nothing rather than pick the first and call it
        # an answer -- but the plural field carries all of them, because that is
        # what the job table stores and what the catalog actually resolved.
        market_id = market_ids[0] if len(market_ids) == 1 else None
        envelope = {
            "title": candidate.get("title", ""),
            "company": candidate.get("company", ""),
            "location": candidate.get("location", ""),
            "url": candidate.get("url", ""),
            "snippet": candidate.get("snippet", ""),
            "salary": candidate.get("salary", ""),
            "date_posted": candidate.get("date_posted", ""),
            "source": candidate.get("source", ""),
            "source_id": candidate.get("source_id", ""),
            "source_type": source_type,
            "discovery_route": route,
            # No search happened here; this is the board API's own language.
            "search_language": languages.get(market_id, "en"),
            "observed_at": observed_at,
            # A public board API lists only postings that are currently open.
            "link_verification_status": "alive",
            "identity_keys": candidate.get("identity_keys") or [],
            "location_normalized": {
                "market_id": market_id,
                "market_ids": market_ids,
                "city_id": location["city_id"],
                "remote_scope": location["remote_scope"],
                "confidence": location["confidence"],
            },
        }
        pairs.append((envelope, {
            "jd_text": candidate.get("jd_text", ""),
            "jd_text_truncated": bool(candidate.get("jd_text_truncated")),
        }))
    return pairs

def _elapsed_since(timestamp: Any, now: datetime) -> timedelta:
    """Age of `timestamp`. An absent or unreadable stamp reads as long ago."""
    try:
        last = datetime.fromisoformat(str(timestamp or ""))
    except ValueError:
        return timedelta.max
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last


def _fetch_due(
    board: dict[str, Any],
    *,
    fetch_interval: timedelta,
    ttl: timedelta,
    now: datetime,
) -> bool:
    """Is this board due to be asked for jobs now?

    Two unrelated questions used to share one answer. Re-verifying that a board
    still exists is monthly work; asking it what it posted today is per-round
    work. Gating the second on the first silenced the cheapest discovery
    channel for `ats_registry_ttl_days` after its first success -- a real run
    reached every board on day one and then reported `boards_attempted: 0` with
    no reason given, for thirty days.
    """
    status = board.get("status")
    if status == "candidate":
        return True
    if status == "unavailable":
        # Back off from a board that keeps 404ing; that is what the TTL is for.
        return _elapsed_since(board.get("last_attempt_at"), now) >= ttl
    return _elapsed_since(board.get("last_success_at"), now) >= fetch_interval


def _bounded_number(
    config: dict[str, Any], key: str, default: float, minimum: float, maximum: float
) -> float:
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError) as error:
        raise AtsPipelineError(f"{key} must be numeric") from error
    if not minimum <= value <= maximum:
        raise AtsPipelineError(f"{key} must be between {minimum:g} and {maximum:g}")
    return value


def _fill_deferred_jd(
    selected: list[tuple[dict[str, Any], dict[str, Any]]],
    eligible: list[dict[str, Any]],
    *,
    provider_client: AtsProvider,
    timeout_seconds: float,
    request_budget: RequestBudget,
    max_concurrency: int,
) -> dict[str, dict[str, Any]]:
    """Fetch the descriptions the kept postings are missing, per board.

    Grouped by board and serial within one, because these are one host's
    postings and the listing they replaced was a single request. Boards run
    concurrently under the same cap and the same request budget as the
    listings, so the second pass cannot outspend the first.
    """
    by_board: dict[str, list[dict[str, Any]]] = {}
    for board, job in selected:
        if job.get("jd_text"):
            continue
        by_board.setdefault(board["board_id"], []).append(job)
    if not by_board:
        return {}
    boards_by_id = {board["board_id"]: board for board in eligible}

    def run(item: tuple[str, list[dict[str, Any]]]) -> tuple[str, dict[str, Any]]:
        board_id, jobs = item
        return board_id, fetch_job_content(
            boards_by_id[board_id],
            jobs,
            provider_client=provider_client,
            timeout_seconds=timeout_seconds,
            request_budget=request_budget,
        )

    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        return dict(executor.map(run, sorted(by_board.items())))


def _safe_state_row(
    board: dict[str, Any],
    metrics: dict[str, Any],
    filtered: int,
    out_of_market: int = 0,
) -> dict[str, Any]:
    return {
        "board_id": board["board_id"],
        "provider": metrics.get("provider", board.get("provider", "unknown")),
        "action": "sync",
        "status": str(board.get("status") or "candidate"),
        "ok": bool(metrics.get("ok")),
        "requests": int(metrics.get("requests", 0)),
        "pages_requested": int(metrics.get("pages_requested", 0)),
        "response_bytes": int(metrics.get("response_bytes", 0)),
        "jobs_received": int(metrics.get("jobs_received", 0)),
        "jobs_normalized": int(metrics.get("jobs_normalized", 0)),
        "jobs_with_jd": int(metrics.get("jobs_with_jd", 0)),
        "jd_text_truncated": int(metrics.get("jd_text_truncated", 0)),
        "jobs_prefiltered": filtered,
        # Without this, a board with nothing in this market and a board with
        # nothing matching the role both report the same single zero.
        "jobs_out_of_market": out_of_market,
        # Present on every row whether or not this board deferred anything, so
        # a board that fetched no descriptions and a board whose second pass
        # was never recorded do not read the same.
        "jd_requests": 0,
        "jd_fetch_failed": 0,
        "jd_fetch_skipped": 0,
        "truncated": bool(metrics.get("truncated")),
        "rate_limited": bool(metrics.get("rate_limited")),
        "content_fallback": bool(metrics.get("content_fallback")),
        "content_deferred": bool(metrics.get("content_deferred")),
        "failure_kind": str(metrics.get("failure_kind") or ""),
        "http_status": metrics.get("http_status"),
        "duration_ms": float(metrics.get("duration_ms", 0)),
        "attempted_at": _now().isoformat(),
    }


def sync_registry(
    registry: dict[str, Any],
    profile: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    provider_client: AtsProvider | None = None,
    metrics_run_id: str | None = None,
    board_ids: set[str] | None = None,
    markets_by_board: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Synchronize eligible boards and return bounded merge-ready candidates.

    `board_ids` restricts the run to those boards. A discovery wave plans a
    specific set of structured tasks, and a candidate from a board outside that
    set has no task to be attributed to.

    `markets_by_board` scopes each board to the markets its task plans. Without
    it the channel keeps every country the board lists, which is right for
    registry maintenance and wrong for a market-scoped round; a board that is
    fetched but missing from the mapping is an error rather than a board with
    no scope.
    """
    cfg = config or load_config()
    if not cfg.get("ats_enabled", False):
        return {"ok": True, "status": "disabled", "candidates": [], "summary": {"boards": 0}}
    boards = registry.get("boards")
    if not isinstance(boards, list):
        raise AtsPipelineError("ATS registry boards must be a list")
    now = _now()
    ttl_days = int(_bounded_number(cfg, "ats_registry_ttl_days", 30, 1, 365))
    # How soon a verified board may be asked for jobs again. Minutes, not days:
    # this is job freshness, not registry freshness. 0 means every round.
    fetch_interval_minutes = int(
        _bounded_number(cfg, "ats_fetch_interval_minutes", 60, 0, 10080)
    )
    # Hard ceilings, not defaults: they bound what this pipeline may ask of a
    # public board host. Raised with the catalog -- one market alone now seeds
    # more than ten boards, and a measured board costs about one request and a
    # fraction of a second, so the old ceilings capped the cheapest channel
    # below the size of its own source list.
    boards_per_round = int(_bounded_number(cfg, "ats_boards_per_round", 10, 1, 60))
    requests_per_round = int(_bounded_number(cfg, "ats_requests_per_round", 30, 1, 100))
    page_size = int(_bounded_number(cfg, "ats_page_size", 50, 1, 100))
    max_pages = int(_bounded_number(cfg, "ats_max_pages", 10, 1, 10))
    timeout_seconds = _bounded_number(cfg, "ats_timeout_seconds", 30, 1, 60)
    max_concurrency = int(_bounded_number(cfg, "ats_max_concurrency", 3, 1, 3))
    # Read the listings without their job descriptions and fetch the
    # descriptions afterwards, for the postings this round keeps. Off restores
    # the single-request-per-board shape for a caller that wants every
    # description regardless of what the round selects.
    defer_jd = cfg.get("ats_defer_jd", True) is not False
    selectable = [
        board for board in boards
        if isinstance(board, dict)
        and board.get("enabled") is True
        and board.get("status") in {"candidate", "verified", "unavailable"}
        and (board_ids is None or board.get("board_id") in board_ids)
    ]
    due = [
        board for board in selectable
        if _fetch_due(
            board,
            fetch_interval=timedelta(minutes=fetch_interval_minutes),
            ttl=timedelta(days=ttl_days),
            now=now,
        )
    ]
    # Least recently fetched first, so the per-round cap rotates through the
    # catalog instead of always serving the same head of the list.
    due.sort(key=lambda board: (
        board.get("status") != "verified",
        str(board.get("last_success_at") or ""),
        board["board_id"],
    ))
    eligible = due[:boards_per_round]
    boards_skipped_not_due = len(selectable) - len(due)
    boards_skipped_by_cap = len(due) - len(eligible)
    budget = RequestBudget(requests_per_round)
    client = provider_client or HttpAtsProvider()

    def run(board: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return fetch_board(
            board,
            provider_client=client,
            page_size=page_size,
            max_pages=max_pages,
            timeout_seconds=timeout_seconds,
            request_budget=budget,
            defer_content=defer_jd,
        )

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        results = list(executor.map(run, eligible))

    state_rows: list[dict[str, Any]] = []
    filtered_by_board: list[list[dict[str, Any]]] = []
    market_resources = (
        market_plan.load_resources()[0] if markets_by_board is not None else {}
    )
    for board, (metrics, jobs) in zip(eligible, results):
        board["last_attempt_at"] = now.isoformat()
        if metrics.get("ok"):
            board["status"] = "verified"
            board["last_success_at"] = now.isoformat()
            board["consecutive_unavailable"] = 0
        elif metrics.get("http_status") in _UNAVAILABLE_STATUSES:
            board["consecutive_unavailable"] = int(board.get("consecutive_unavailable") or 0) + 1
            if board["consecutive_unavailable"] >= 3:
                board["status"] = "unavailable"
        else:
            board["consecutive_unavailable"] = 0
        filtered = prefilter_jobs(jobs, profile)
        if markets_by_board is None:
            in_market = filtered
        else:
            planned = markets_by_board.get(board["board_id"])
            if planned is None:
                raise AtsPipelineError(
                    "a fetched board has no planned markets: "
                    f"{board['board_id']}"
                )
            in_market = filter_to_markets(
                filtered, planned, resources=market_resources
            )
        filtered_by_board.append(in_market)
        state_rows.append(
            _safe_state_row(
                board, metrics, len(in_market), len(filtered) - len(in_market)
            )
        )

    candidate_limit = min(
        100,
        int(_bounded_number(cfg, "top_n", 15, 1, 100))
        + int(_bounded_number(cfg, "precise_buffer", 5, 0, 100)),
    )
    # Selection first, descriptions second. The cap is what makes deferring
    # them worth anything: the round keeps at most `candidate_limit` postings,
    # so fetching each kept description is bounded by that number rather than
    # by how many jobs the boards happen to list.
    seen_identities: set[str] = set()
    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    # Level fit first, then breadth. Every posting the CV is eligible for is
    # offered before any stretch posting, and those before the rest -- so a
    # senior role reaches the cap only when slots remain, instead of taking
    # them because its board came first (see `level_tier`).
    tiers = [[[], [], []] for _ in filtered_by_board]
    for board_tiers, jobs in zip(tiers, filtered_by_board):
        for job in jobs:
            board_tiers[level_tier(str(job.get("title") or ""), profile)].append(job)
    # Within a tier, one posting per board per pass, rather than filling the cap
    # from the first boards in the list. A board with 1,799 postings used to
    # take every slot it could reach: measured 2026-09-27, three boards of
    # thirty-two filled all twenty candidate slots and the other twenty-nine
    # contributed nothing, so the round's breadth was decided by catalog order.
    # Within one board the order it returned is kept.
    for tier in range(3):
        queues = [list(board_tiers[tier]) for board_tiers in tiers]
        while len(selected) < candidate_limit and any(queues):
            progressed = False
            for board, queue in zip(eligible, queues):
                if not queue or len(selected) >= candidate_limit:
                    continue
                job = queue.pop(0)
                progressed = True
                identity = str((job.get("identity_keys") or [""])[0])
                if not identity or identity in seen_identities:
                    continue
                seen_identities.add(identity)
                selected.append((board, job))
            if not progressed:
                break

    jd_rows_by_board = _fill_deferred_jd(
        selected,
        eligible,
        provider_client=client,
        timeout_seconds=timeout_seconds,
        request_budget=budget,
        max_concurrency=max_concurrency,
    ) if defer_jd else {}

    emitted: list[dict[str, Any]] = []
    emitted_by_board: dict[str, int] = {}
    emitted_with_jd_by_board: dict[str, int] = {}
    for board, job in selected:
        board_id = board["board_id"]
        emitted.append(_clean_candidate(job, board_id))
        emitted_by_board[board_id] = emitted_by_board.get(board_id, 0) + 1
        if job.get("jd_text"):
            emitted_with_jd_by_board[board_id] = (
                emitted_with_jd_by_board.get(board_id, 0) + 1
            )

    for row in state_rows:
        jd_metrics = jd_rows_by_board.get(row["board_id"])
        if jd_metrics is None:
            continue
        # The second pass is the same board's traffic, so it lands on the same
        # row rather than in a channel total nobody can attribute.
        row["requests"] += jd_metrics["requests"]
        row["response_bytes"] += jd_metrics["response_bytes"]
        row["jd_requests"] = jd_metrics["requests"]
        row["jd_fetch_failed"] = jd_metrics["jobs_failed"]
        row["jd_fetch_skipped"] = jd_metrics["jobs_skipped"]
        row["jobs_with_jd"] += jd_metrics["jobs_filled"]
        row["jd_text_truncated"] += jd_metrics["jd_text_truncated"]
        if jd_metrics["rate_limited"]:
            row["rate_limited"] = True
        # Not `failure_kind`: that field says why this board failed, and a
        # board whose listing answered has not failed because one of its
        # descriptions did not. The count carries that.

    metrics_recorded = True
    for row in state_rows:
        row["jobs_emitted"] = emitted_by_board.get(row["board_id"], 0)
        row["jobs_with_jd_emitted"] = emitted_with_jd_by_board.get(row["board_id"], 0)
        metric_values = {key: value for key, value in row.items() if key not in {"board_id", "ok", "attempted_at"}}
        metrics_recorded = (
            record_metric(
                METRICS_PATH,
                "ats",
                bool(row["ok"]),
                run_id=metrics_run_id,
                **metric_values,
            )
            and metrics_recorded
        )

    registry["schema_version"] = 1
    _save_ats_registry(registry)
    state = {
        "schema_version": 1,
        "boards": state_rows,
        "summary": {
            "boards_attempted": len(eligible),
            # Without these two a round that fetched nothing is indistinguishable
            # from a round where every board answered with nothing.
            "boards_skipped_not_due": boards_skipped_not_due,
            "boards_skipped_by_cap": boards_skipped_by_cap,
            "boards_succeeded": sum(row["ok"] for row in state_rows),
            "boards_failed": sum(not row["ok"] for row in state_rows),
            "requests": budget.used,
            "response_bytes": sum(row["response_bytes"] for row in state_rows),
            "jobs_received": sum(row["jobs_received"] for row in state_rows),
            "jobs_normalized": sum(row["jobs_normalized"] for row in state_rows),
            "jobs_with_jd": sum(row["jobs_with_jd"] for row in state_rows),
            "jobs_with_jd_emitted": sum(
                row["jobs_with_jd_emitted"] for row in state_rows
            ),
            "jd_text_truncated": sum(row["jd_text_truncated"] for row in state_rows),
            "jobs_prefiltered": sum(row["jobs_prefiltered"] for row in state_rows),
            "jobs_out_of_market": sum(
                row["jobs_out_of_market"] for row in state_rows
            ),
            "jobs_emitted": len(emitted),
            "jd_requests": sum(row["jd_requests"] for row in state_rows),
            "jd_fetch_failed": sum(row["jd_fetch_failed"] for row in state_rows),
            "jd_fetch_skipped": sum(row["jd_fetch_skipped"] for row in state_rows),
            "content_fallback_boards": sum(
                row["content_fallback"] for row in state_rows
            ),
            "content_deferred_boards": sum(
                row["content_deferred"] for row in state_rows
            ),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        },
    }
    _save_document(SYNC_STATE_PATH, state)
    return {
        "ok": True,
        "status": "completed",
        "candidates": emitted,
        # Per board, so a caller can report one task outcome per planned board
        # instead of narrating a single number for the whole channel.
        "boards": state_rows,
        "summary": state["summary"],
        "metrics_recorded": metrics_recorded,
    }


def _read_stdin_list() -> list[Any]:
    payload = json.loads(read_stdin_text() or "[]")
    if not isinstance(payload, list):
        raise AtsPipelineError("stdin must be a JSON array")
    return payload


def _read_profile(path: Path) -> dict[str, Any]:
    payload = _load_document(path, {})
    if not payload:
        raise AtsPipelineError("profile must be a non-empty JSON object")
    return payload


def main() -> int:
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("discover")
    sync_parser = subparsers.add_parser("sync")
    sync_parser.add_argument("--profile", type=Path, required=True)
    sync_parser.add_argument("--metrics-run-id", type=validate_run_id)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--profile", type=Path, required=True)
    run_parser.add_argument("--metrics-run-id", type=validate_run_id)
    args = parser.parse_args()
    try:
        registry = _load_ats_registry()
        discovery = None
        if args.command in {"discover", "run"}:
            discovery = discover_candidates(_read_stdin_list(), registry)
            _save_ats_registry(registry)
        if args.command == "discover":
            print(json.dumps({"ok": True, **(discovery or {})}, ensure_ascii=False))
            return 0
        result = sync_registry(
            registry,
            _read_profile(args.profile),
            metrics_run_id=args.metrics_run_id,
        )
        if discovery is not None:
            result["discovery"] = discovery
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (AtsPipelineError, StdinUnavailable, json.JSONDecodeError) as error:
        record_metric(
            METRICS_PATH,
            "ats",
            False,
            run_id=getattr(args, "metrics_run_id", None),
            failure_kind="input_validation",
        )
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
