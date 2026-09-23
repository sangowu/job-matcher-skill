#!/usr/bin/env python3
"""从已发现职位的 URL 反推公开 ATS board，复验后写入来源注册表。

任何来源（Web Search、浏览器、已有主表）找到的一个 Ashby/Greenhouse/Lever 职位，
其 URL 里都带着该公司 board 的标识。把它取出来复验一次，整家公司的职位下一轮
就能用一次公开 API 请求取回 —— 一个职位换一整家公司。

手工策展只负责冷启动；目录靠这条路径增长。

公司把 board 嵌进自家招聘页时，URL 里没有厂商域名，也没有 board token——
只有 provider 和 job id。这时 token 从主机名猜，再用那个 job id 去猜出的 board 上
验证：job id 在，token 才成立。猜错会落到别家公司的 board，所以"有应答"不算数。

隐私边界：只读取候选的 `url` 字段用于反推 `(provider, board_token)`；job id 仅用于
当场验证，用完即弃。写入注册表的是 provider、token 和低基数计数，
绝不写 URL、职位名、JD 或 CV。

用法:
    python scripts/board_harvest.py --candidates candidates.json [--limit N]
        [--hint-limit N] [--dry-run]

stdin 也可作为候选输入。输出只含计数。
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ats_provider  # noqa: E402
import source_registry  # noqa: E402
from _jobutil import extract_board, extract_board_hint  # noqa: E402

MARKETS_PATH = SKILL_ROOT / "references" / "markets.json"
# 采集来的 board 排在人工策展来源之后，除非后续被显式调整。
HARVEST_PRIORITY = 60
# 单次运行的外部复验请求上限，避免一批候选把探测扇出成几十次请求。
DEFAULT_PROBE_LIMIT = 5
# 猜出的 token 每个都要一次请求，所以单独限额，别让一批候选扇出成几十次探测。
DEFAULT_HINT_LIMIT = 3
# 短别名（"IE"、"DE"、"UK"）会命中无关词，按长度剔除。
MIN_ALIAS_LENGTH = 4


class BoardHarvestError(RuntimeError):
    """采集输入或注册表写入失败。"""


def _load_market_aliases(markets_path: Path = MARKETS_PATH) -> list[tuple[str, str]]:
    """构造 `(alias, market_id)`，长别名优先，使 "northern ireland" 胜过 "ireland"。"""
    payload = json.loads(markets_path.read_text(encoding="utf-8"))
    aliases: list[tuple[str, str]] = []
    for market in payload["markets"]:
        market_id = market["market_id"]
        for alias in market.get("country_aliases", []):
            aliases.append((str(alias).strip().lower(), market_id))
        for city in market.get("cities", []):
            for alias in city.get("aliases", []):
                aliases.append((str(alias).strip().lower(), market_id))
            for area in city.get("administrative_areas", []):
                aliases.append((str(area).strip().lower(), market_id))
    aliases = [item for item in aliases if len(item[0]) >= MIN_ALIAS_LENGTH]
    aliases.sort(key=lambda item: -len(item[0]))
    return aliases


def _markets_from_locations(
    candidates: list[dict[str, Any]], aliases: list[tuple[str, str]]
) -> Counter[str]:
    """市场归属由实际职位地点决定，不按公司总部推断。"""
    hits: Counter[str] = Counter()
    for candidate in candidates:
        location = str(candidate.get("location") or "").lower()
        if not location:
            continue
        for alias, market_id in aliases:
            if alias in location:
                hits[market_id] += 1
                break
    return hits


def _search_languages(markets: list[str]) -> list[str]:
    languages = ["en"]
    if "de" in markets:
        languages.append("de")
    if "cn" in markets:
        languages.append("zh-Hans")
    return languages


def extract_boards(candidates: list[Any]) -> dict[tuple[str, str], int]:
    """统计候选 URL 反推出的 board，值为该 board 贡献的候选数。"""
    found: dict[tuple[str, str], int] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        board = extract_board(str(candidate.get("url") or ""))
        if board is None:
            continue
        found[board] = found.get(board, 0) + 1
    return found


def extract_hints(candidates: list[Any]) -> dict[tuple[str, tuple[str, ...]], set[str]]:
    """统计自有域名上的嵌入式 board：`(provider, 候选 token) -> 观察到的 job id`。

    这类 URL 里没有 board token，只有 provider 和 job id。token 是从主机名猜的，
    job id 则是验证它的证据——见 `confirm_board`。
    """
    hints: dict[tuple[str, tuple[str, ...]], set[str]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        hint = extract_board_hint(str(candidate.get("url") or ""))
        if hint is None:
            continue
        provider, job_id, tokens = hint
        hints.setdefault((provider, tuple(tokens)), set()).add(job_id)
    return hints


def confirm_board(
    provider: str,
    tokens: tuple[str, ...],
    job_ids: set[str],
    aliases: list[tuple[str, str]],
    *,
    provider_client: Any = None,
    page_size: int = 50,
    max_pages: int = 2,
    timeout_seconds: float = 20,
) -> tuple[str | None, list[str], int, int]:
    """验证猜出的 token。返回 `(确认的 token, 命中市场, 职位数, 请求数)`。

    一个主机名猜出的 token 完全可能是**别家公司**在同一 ATS 上的 board——
    建目录时就撞到过一次。所以"board 有应答"不算数：必须在它返回的职位里
    找到我们观察到的那个 job id，才证明 token 属于这家公司。
    """
    requests = 0
    for token in tokens:
        board = {"provider": provider, "company": token, "board_token": token}
        if provider == "lever":
            board["instance"] = "global"
        requests += 1
        try:
            metrics, jobs = ats_provider.fetch_board(
                board,
                provider_client=provider_client,
                page_size=page_size,
                max_pages=max_pages,
                timeout_seconds=timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - 单个猜测失败不能中断整批采集
            continue
        if not metrics.get("ok") or not jobs:
            continue
        seen = {str(job.get("provider_job_id") or "").strip().lower() for job in jobs}
        if not (seen & job_ids):
            # board 存在，但不是这家公司的。丢掉，不要猜第二次。
            continue
        hits = _markets_from_locations(jobs, aliases)
        markets = [
            market_id
            for market_id in source_registry.SUPPORTED_MARKETS
            if hits.get(market_id)
        ]
        return token, markets, len(jobs), requests
    return None, [], 0, requests


def _known_source_ids(registry: dict[str, Any], seeds: dict[str, Any]) -> set[str]:
    known = {source["source_id"] for source in registry["sources"]}
    known |= {source["source_id"] for source in seeds["sources"]}
    return known


def probe_board(
    provider: str,
    token: str,
    aliases: list[tuple[str, str]],
    *,
    provider_client: Any = None,
    page_size: int = 50,
    max_pages: int = 2,
    timeout_seconds: float = 20,
) -> tuple[bool, list[str], int]:
    """复验一个 board。返回 `(可达, 命中市场, 职位数)`。

    仅当 board 应答且职位地点落在受支持市场时才算可用；可达不等于有覆盖。
    """
    board = {"provider": provider, "company": token, "board_token": token}
    if provider == "lever":
        board["instance"] = "global"
    try:
        metrics, jobs = ats_provider.fetch_board(
            board,
            provider_client=provider_client,
            page_size=page_size,
            max_pages=max_pages,
            timeout_seconds=timeout_seconds,
        )
    except Exception:  # noqa: BLE001 - 单个 board 失败不能中断整批采集
        return False, [], 0
    if not metrics.get("ok") or not jobs:
        return False, [], len(jobs)
    hits = _markets_from_locations(jobs, aliases)
    markets = [
        market_id
        for market_id in source_registry.SUPPORTED_MARKETS
        if hits.get(market_id)
    ]
    return True, markets, len(jobs)


def harvest(
    candidates: list[Any],
    *,
    registry_path: Path = source_registry.REGISTRY_PATH,
    lock_path: Path | None = None,
    seeds_path: Path = source_registry.SEEDS_PATH,
    markets_path: Path = MARKETS_PATH,
    batch_id: str,
    limit: int = DEFAULT_PROBE_LIMIT,
    hint_limit: int = DEFAULT_HINT_LIMIT,
    enable_verified: bool = True,
    dry_run: bool = False,
    provider_client: Any = None,
) -> dict[str, Any]:
    """反推、复验并提交新的 board。返回只含计数的摘要。"""
    registry = source_registry.load_registry(registry_path)
    seeds = source_registry.load_seeds(seeds_path, markets_path=markets_path)
    aliases = _load_market_aliases(markets_path)
    known = _known_source_ids(registry, seeds)

    found = extract_boards(candidates)
    fresh = [
        (provider, token)
        for (provider, token) in sorted(found)
        if source_registry.board_source_id(provider, token) not in known
    ]
    probed = fresh[: max(0, limit)]

    proposals: list[dict[str, Any]] = []
    events: list[dict[str, str]] = []
    outcomes: Counter[str] = Counter()
    for provider, token in probed:
        reachable, markets, _ = probe_board(
            provider, token, aliases, provider_client=provider_client
        )
        if not reachable:
            outcomes["unreachable_or_empty"] += 1
            continue
        if not markets:
            outcomes["no_supported_market_jobs"] += 1
            continue
        source_id = source_registry.board_source_id(provider, token)
        proposals.append(
            {
                "source_id": source_id,
                "display_name": token,
                "source_type": "ats_board",
                "provider": provider,
                "board_token": token,
                "markets": markets,
                "search_languages": _search_languages(markets),
                "access_methods": ["ats_public_api"],
                "verification_ttl_days": 30,
                "priority": HARVEST_PRIORITY,
            }
        )
        # 复验本身就是证据：探测成功才 verified，随后才允许 enable。
        events.append({"source_id": source_id, "outcome": "verified"})
        if enable_verified:
            events.append({"source_id": source_id, "outcome": "enable"})
        outcomes["verified"] += 1

    # 第二遍：自有域名上的嵌入式 board。token 是猜的，必须靠 job id 验证。
    proposed_ids = {proposal["source_id"] for proposal in proposals}
    hints = extract_hints(candidates)
    hint_requests = 0
    hints_confirmed = 0
    hints_attempted = 0
    for (provider, tokens), job_ids in sorted(hints.items()):
        if hints_attempted >= max(0, hint_limit):
            break
        # 猜测里只要有一个已经是已知来源，就不必再去探测这家公司。
        if any(
            source_registry.board_source_id(provider, token) in known | proposed_ids
            for token in tokens
        ):
            outcomes["hint_already_known"] += 1
            continue
        hints_attempted += 1
        token, markets, _, requests = confirm_board(
            provider, tokens, job_ids, aliases, provider_client=provider_client
        )
        hint_requests += requests
        if token is None:
            outcomes["hint_unconfirmed"] += 1
            continue
        if not markets:
            outcomes["hint_no_supported_market_jobs"] += 1
            continue
        source_id = source_registry.board_source_id(provider, token)
        if source_id in known | proposed_ids:
            outcomes["hint_already_known"] += 1
            continue
        proposals.append(
            {
                "source_id": source_id,
                "display_name": token,
                "source_type": "ats_board",
                "provider": provider,
                "board_token": token,
                "markets": markets,
                "search_languages": _search_languages(markets),
                "access_methods": ["ats_public_api"],
                "verification_ttl_days": 30,
                "priority": HARVEST_PRIORITY,
            }
        )
        proposed_ids.add(source_id)
        events.append({"source_id": source_id, "outcome": "verified"})
        if enable_verified:
            events.append({"source_id": source_id, "outcome": "enable"})
        outcomes["hint_confirmed"] += 1
        hints_confirmed += 1

    summary: dict[str, Any] = {
        "schema_version": 1,
        "candidates_read": len(candidates),
        "boards_seen": len(found),
        "boards_already_known": len(found) - len(fresh),
        "boards_probed": len(probed),
        "boards_deferred_by_limit": max(0, len(fresh) - len(probed)),
        "hints_seen": len(hints),
        "hints_attempted": hints_attempted,
        "hints_confirmed": hints_confirmed,
        "hint_requests": hint_requests,
        "hints_deferred_by_limit": max(0, len(hints) - hints_attempted),
        "probe_outcomes": dict(sorted(outcomes.items())),
        "boards_proposed": len(proposals),
        "enable_requested": bool(enable_verified and proposals),
        "applied": False,
    }
    if dry_run or not proposals:
        return summary

    try:
        result = source_registry.apply_batch_to_registry(
            {"batch_id": batch_id, "proposals": proposals, "events": events},
            registry_path=registry_path,
            lock_path=lock_path or registry_path.with_name("source_registry.lock"),
        )
    except source_registry.SourceRegistryError as error:
        raise BoardHarvestError(f"cannot commit harvested boards: {error}") from error
    summary["applied"] = True
    summary["idempotent"] = bool(result.get("idempotent"))
    summary["proposals_added"] = int(result.get("proposals_added", 0))
    summary["events_applied"] = int(result.get("events_applied", 0))
    return summary


def _read_candidates(path: Path | None) -> list[Any]:
    raw = (
        path.read_text(encoding="utf-8")
        if path is not None
        else sys.stdin.buffer.read().decode("utf-8", errors="replace")
    )
    if not raw.strip():
        return []
    payload = json.loads(raw)
    if isinstance(payload, dict):
        payload = payload.get("candidates", [])
    if not isinstance(payload, list):
        raise BoardHarvestError("candidates must be a list or {\"candidates\": [...]}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--limit", type=int, default=DEFAULT_PROBE_LIMIT)
    parser.add_argument("--hint-limit", type=int, default=DEFAULT_HINT_LIMIT)
    parser.add_argument("--registry", type=Path, default=source_registry.REGISTRY_PATH)
    parser.add_argument("--no-enable", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        candidates = _read_candidates(args.candidates)
        batch_id = args.batch_id or f"harvest-{uuid.uuid4().hex}"
        summary = harvest(
            candidates,
            registry_path=args.registry,
            batch_id=batch_id,
            limit=args.limit,
            hint_limit=args.hint_limit,
            enable_verified=not args.no_enable,
            dry_run=args.dry_run,
        )
    except (BoardHarvestError, source_registry.SourceRegistryError, json.JSONDecodeError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, "summary": summary}, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
