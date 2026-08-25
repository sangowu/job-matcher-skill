#!/usr/bin/env python3
"""Deterministic state helpers for bounded visual-browser listing pagination."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable


PAGE_STATUSES = ("ok", "user_action_required", "rate_limited", "failed")


@dataclass(frozen=True)
class PageObservation:
    status: str
    links: list[str]
    has_next_page: bool

    def __post_init__(self) -> None:
        if self.status not in PAGE_STATUSES:
            raise ValueError(f"invalid page status: {self.status}")


@dataclass(frozen=True)
class ListingResult:
    status: str
    links: list[str]
    pages_visited: int


@dataclass(frozen=True)
class HandoffWindow:
    started_at: datetime
    timeout_minutes: int

    def __post_init__(self) -> None:
        if self.timeout_minutes <= 0:
            raise ValueError("timeout_minutes must be positive")
        if self.started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")

    @classmethod
    def start(cls, timeout_minutes: int) -> "HandoffWindow":
        return cls(datetime.now(timezone.utc), timeout_minutes)

    def status_at(self, current: datetime) -> str:
        if current.tzinfo is None:
            raise ValueError("current must be timezone-aware")
        deadline = self.started_at + timedelta(minutes=self.timeout_minutes)
        return "resumed" if current <= deadline else "timeout"


def collect_listing_pages(
    *,
    inspect_page: Callable[[int], PageObservation],
    advance_page: Callable[[int], None],
    max_pages: int,
) -> ListingResult:
    """Inspect one site sequentially, deduplicate links, and stop at the cap."""
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    links: list[str] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        observation = inspect_page(page)
        for link in observation.links:
            if isinstance(link, str) and link and link not in seen:
                seen.add(link)
                links.append(link)
        if observation.status != "ok":
            return ListingResult(observation.status, links, page)
        if not observation.has_next_page or page == max_pages:
            return ListingResult("ok", links, page)
        advance_page(page + 1)
    raise AssertionError("unreachable")
