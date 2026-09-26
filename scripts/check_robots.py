#!/usr/bin/env python3
"""Answer, for one URL, what a site's robots.txt actually permits.

The catalog's guardrail says to respect robots/ToS, and until now nothing here
could read a robots.txt at all: ten `company_careers` seeds carried
`verified: true` and `automation_allowed: true` on no evidence beyond one
employer having been looked at by hand.

Why not `urllib.robotparser`: it returns the FIRST matching rule in file order.
RFC 9309 section 2.2.2 says the MOST SPECIFIC match wins -- the longest matching
path -- with a tie going to Allow. The difference decides real cases. Microsoft's
careers host publishes

    User-agent: *
    Disallow: /
    Allow: /careers

which the stdlib reads as "everything is forbidden" and which actually says
"/careers is allowed". Reading it the stdlib way would have had this catalog
refuse a source its operator deliberately opened, and "be conservative" is no
excuse for getting the answer wrong: a wrong refusal is still a wrong reading.

Nothing here disguises a request, retries around a rule, or looks for a group
with softer rules than the one that applies. It reads the file the site
published for this question and reports what it says.

Usage:
  python check_robots.py --url https://example.com/careers/search
  python check_robots.py --catalog            # every seed's entry_url
  python check_robots.py --catalog --also-query 'q=ai+engineer&page=2'

Output: one JSON object; `--catalog` reports a list under `results`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlparse

from _jobutil import SKILL_ROOT
from _stdio import use_utf8_stdout


SEEDS_PATH = SKILL_ROOT / "references" / "source_seeds.json"
# Says who is asking and why. Not a disguise: the point of naming yourself is
# that a site can tell you apart and block you if it wants to.
USER_AGENT = "job-matcher-skill/1.0 (+individual job seeker; one request per host)"
# Between two hosts there is no shared queue, but a person running this over a
# catalog should still not open ten sockets at once.
DEFAULT_INTERVAL_SECONDS = 5.0


def _pattern(path: str) -> re.Pattern[str]:
    """A robots path pattern: `*` matches any run, a trailing `$` anchors the end."""
    anchored = path.endswith("$")
    body = path[:-1] if anchored else path
    expression = "".join(
        ".*" if part == "*" else re.escape(part) for part in re.split(r"(\*)", body)
    )
    return re.compile(f"^{expression}$" if anchored else f"^{expression}")


def parse_groups(body: str) -> dict[str, list[tuple[str, str]]]:
    """user-agent -> [(allow|disallow, path)].

    Consecutive `User-agent` lines share one group, which is how a file says
    "these agents, same rules". A `User-agent` line after a rule line starts a
    new group instead.
    """
    found: dict[str, list[tuple[str, str]]] = {}
    agents: list[str] = []
    collecting_agents = True
    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if not collecting_agents:
                agents = []
                collecting_agents = True
            agents.append(value.lower())
            found.setdefault(value.lower(), [])
        elif field in {"allow", "disallow"} and agents:
            collecting_agents = False
            for agent in agents:
                found[agent].append((field, value))
    return found


def crawl_delay(body: str, agent: str = "*") -> float | None:
    """Seconds the site asks a client to wait between requests, if it says.

    Not part of the original standard and not in RFC 9309, but widely published
    and unambiguous when present: `publicjobs.tal.net` asks for ten seconds,
    which is twice the pacing floor this repo would otherwise use. A site that
    writes down its own pace has answered a question we would otherwise be
    guessing at.
    """
    group_agents: list[str] = []
    collecting = True
    best: float | None = None
    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        if field == "user-agent":
            if not collecting:
                group_agents = []
                collecting = True
            group_agents.append(value.strip().lower())
        elif field in {"allow", "disallow"}:
            collecting = False
        elif field == "crawl-delay" and agent.lower() in group_agents:
            collecting = False
            try:
                seconds = float(value.strip())
            except ValueError:
                continue
            if seconds > 0 and (best is None or seconds > best):
                best = seconds
    if best is None and agent != "*":
        return crawl_delay(body, "*")
    return best


def evaluate(body: str, url: str, agent: str = "*") -> dict[str, object]:
    """Whether `url` is allowed, and which line decided it."""
    groups = parse_groups(body)
    group = groups.get(agent.lower())
    if group is None:
        group = groups.get("*", [])
    parsed = urlparse(url)
    path = unquote(parsed.path) or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    best: tuple[int, bool, str] | None = None
    for kind, value in group:
        if kind == "disallow" and value == "":
            # `Disallow:` with nothing after it is the explicit "allow all".
            continue
        if not _pattern(value).match(path):
            continue
        candidate = (len(value), kind == "allow", f"{kind}: {value}")
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is None:
        return {"allowed": True, "rule": "no matching rule", "agent": agent}
    return {"allowed": best[1], "rule": best[2], "agent": agent}


def fetch_robots(origin: str, *, timeout: float = 20.0) -> dict[str, object]:
    """Read one origin's robots.txt.

    A 404 means no rules were published, which RFC 9309 reads as no
    restrictions -- not as permission granted by silence, and not as a refusal
    either. Anything else unanswered leaves the question open rather than
    guessing in whichever direction is convenient.
    """
    url = f"{origin.rstrip('/')}/robots.txt"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {
                "robots_url": url,
                "http_status": response.status,
                "body": response.read().decode("utf-8", "replace"),
            }
    except urllib.error.HTTPError as error:
        return {"robots_url": url, "http_status": error.code, "body": ""}
    except Exception as error:  # noqa: BLE001 - the reason is reported, not raised
        return {
            "robots_url": url,
            "http_status": None,
            "body": "",
            "error": f"{type(error).__name__}",
        }


def check_url(url: str, *, agent: str = "*", timeout: float = 20.0) -> dict[str, object]:
    parsed = urlparse(url)
    fetched = fetch_robots(f"{parsed.scheme}://{parsed.netloc}", timeout=timeout)
    result: dict[str, object] = {
        "url": url,
        "host": parsed.netloc,
        "robots_url": fetched["robots_url"],
        "http_status": fetched["http_status"],
    }
    if fetched.get("error"):
        result["error"] = fetched["error"]
    if fetched["http_status"] == 200:
        result.update(evaluate(str(fetched["body"]), url, agent))
        result["crawl_delay_seconds"] = crawl_delay(str(fetched["body"]), agent)
    elif fetched["http_status"] == 404:
        result.update({"allowed": True, "rule": "no robots.txt published", "agent": agent})
    else:
        # Undetermined, not allowed. The same asymmetry `verify_jobs` keeps: a
        # refusal to answer is not an answer.
        result.update({"allowed": None, "rule": "robots.txt unreadable", "agent": agent})
    return result


def check_catalog(
    seeds_path: Path,
    *,
    source_type: str | None = None,
    also_query: str | None = None,
    agent: str = "*",
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    sleep=time.sleep,
) -> list[dict[str, object]]:
    seeds = json.loads(seeds_path.read_text(encoding="utf-8"))
    rows = [
        source
        for source in seeds.get("sources", [])
        if source.get("entry_url")
        and (source_type is None or source.get("source_type") == source_type)
    ]
    results: list[dict[str, object]] = []
    for index, source in enumerate(rows):
        if index and interval_seconds > 0:
            sleep(interval_seconds)
        entry = str(source["entry_url"])
        fetched = fetch_robots("{0.scheme}://{0.netloc}".format(urlparse(entry)))
        record: dict[str, object] = {
            "source_id": source["source_id"],
            "source_type": source.get("source_type"),
            "entry_url": entry,
            "http_status": fetched["http_status"],
            "automation_allowed": bool(source.get("automation_allowed")),
        }
        body = str(fetched["body"])
        for label, target in (
            ("entry", entry),
            ("with_query", f"{entry}{'&' if urlparse(entry).query else '?'}{also_query}"
                if also_query else None),
        ):
            if target is None:
                continue
            if fetched["http_status"] == 200:
                record[label] = evaluate(body, target, agent)
                record["crawl_delay_seconds"] = crawl_delay(body, agent)
            elif fetched["http_status"] == 404:
                record[label] = {"allowed": True, "rule": "no robots.txt published"}
            else:
                record[label] = {"allowed": None, "rule": "robots.txt unreadable"}
        # The catalog claims automation is fine; robots is the site's answer.
        entry_verdict = record.get("entry", {})
        record["catalog_disagrees"] = bool(
            record["automation_allowed"] and entry_verdict.get("allowed") is False
        )
        results.append(record)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="one URL to ask about")
    group.add_argument(
        "--catalog", action="store_true", help="every seed's entry_url"
    )
    parser.add_argument("--source-type", help="limit --catalog to one source_type")
    parser.add_argument(
        "--also-query",
        help="query string to append, so the search URL is checked and not only "
        "the landing page. Accenture allows /careers/jobsearch and disallows "
        "/careers/jobsearch? -- the landing page is not the search.",
    )
    parser.add_argument("--agent", default="*", help="user-agent group to read")
    parser.add_argument(
        "--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS
    )
    parser.add_argument("--seeds", type=Path, default=SEEDS_PATH)
    args = parser.parse_args(argv)
    use_utf8_stdout()

    if args.url:
        payload: dict[str, object] = {"ok": True, **check_url(args.url, agent=args.agent)}
    else:
        results = check_catalog(
            args.seeds,
            source_type=args.source_type,
            also_query=args.also_query,
            agent=args.agent,
            interval_seconds=args.interval_seconds,
        )
        payload = {
            "ok": True,
            "checked": len(results),
            "catalog_disagrees": [
                row["source_id"] for row in results if row["catalog_disagrees"]
            ],
            "results": results,
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
