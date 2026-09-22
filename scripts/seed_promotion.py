#!/usr/bin/env python3
"""把运行时注册表里已复验的采集来源提升进版本控制的种子目录。

`data/source_registry.json` 在 `.gitignore` 里（它和 CV、职位表、报告同目录，
按 PII 规则整体屏蔽）。因此 `board_harvest.py` 的积累只存在于**本机**：
换机器、重装 skill 就清零，别的用户也享受不到。

board token 是公开信息，不含任何 PII，只是被那条 PII 规则连坐了。本脚本把其中
已复验、未过期的部分提升进 `references/source_seeds.json`，让积累能随仓库分发。

只提升能确定性重建 `entry_url` 的来源（即公开 ATS board）。注册表按设计不存 URL，
所以 URL 无法重建的来源不可提升，只记计数。

提升是所有权转移：写入种子后，本机注册表里对应记录的 origin 改为 `seed`，
否则下一次 `merge_seeds()` 会因为"种子与 agent 来源撞号"而报错。

用法:
    python scripts/seed_promotion.py [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import source_registry  # noqa: E402

PROMOTABLE_TYPES = {"ats_board"}
VERIFICATION_METHOD = "public_ats_api"


class SeedPromotionError(RuntimeError):
    """提升输入或写入失败。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _inline(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(value) for value in values) + "]"


def _expired(source: dict[str, Any], now: datetime) -> bool:
    reference = source.get("last_success_at") or source.get("verified_at")
    if not reference:
        return True
    moment = datetime.fromisoformat(str(reference).replace("Z", "+00:00"))
    age_days = (now - moment.astimezone(timezone.utc)).total_seconds() / 86400
    return age_days > int(source.get("verification_ttl_days", 30))


def selectable(
    registry: dict[str, Any],
    seeds: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """挑出可提升的来源，并按原因统计被跳过的部分。"""
    moment = now or _now()
    seeded = {source["source_id"] for source in seeds["sources"]}
    chosen: list[dict[str, Any]] = []
    skipped = {
        "already_seeded": 0,
        "not_agent_origin": 0,
        "not_verified": 0,
        "verification_expired": 0,
        "no_derivable_entry_url": 0,
    }
    for source in sorted(registry["sources"], key=lambda item: item["source_id"]):
        if source["source_id"] in seeded:
            skipped["already_seeded"] += 1
            continue
        if source.get("origin") != "agent":
            skipped["not_agent_origin"] += 1
            continue
        if source.get("status") != "verified" or not source.get("markets"):
            skipped["not_verified"] += 1
            continue
        if _expired(source, moment):
            skipped["verification_expired"] += 1
            continue
        entry_url = (
            source_registry.board_entry_url(source["provider"], source.get("board_token", ""))
            if source.get("source_type") in PROMOTABLE_TYPES
            else None
        )
        if entry_url is None:
            skipped["no_derivable_entry_url"] += 1
            continue
        chosen.append({**source, "entry_url": entry_url})
    return chosen, skipped


def render_seed(source: dict[str, Any]) -> str:
    """按种子文件既有的手写风格渲染一条记录（紧凑数组、两空格缩进）。"""
    lines = [
        "    {",
        f'      "source_id": "{source["source_id"]}",',
        f'      "display_name": {json.dumps(source["display_name"], ensure_ascii=False)},',
        f'      "source_type": "{source["source_type"]}",',
        f'      "provider": "{source["provider"]}",',
        f'      "board_token": "{source["board_token"]}",',
    ]
    if "instance" in source:
        lines.append(f'      "instance": "{source["instance"]}",')
    lines += [
        f'      "entry_url": "{source["entry_url"]}",',
        f'      "markets": {_inline(list(source["markets"]))},',
        f'      "search_languages": {_inline(list(source["search_languages"]))},',
        '      "enabled": true,',
        '      "verified": true,',
        f'      "verification_method": "{VERIFICATION_METHOD}",',
        f'      "verified_at": "{source["verified_at"]}",',
        f'      "verification_ttl_days": {int(source["verification_ttl_days"])},',
        f'      "priority": {int(source["priority"])},',
        f'      "access_methods": {_inline(list(source["access_methods"]))},',
        '      "automation_allowed": true',
        "    }",
    ]
    return "\n".join(lines)


def _append_seeds(seeds_path: Path, blocks: list[str]) -> None:
    text = seeds_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    marker = "\n    }\n  ]\n}"
    if text.count(marker) != 1:
        raise SeedPromotionError("unexpected source_seeds.json tail")
    text = text.replace(marker, "\n    },\n" + ",\n".join(blocks) + "\n  ]\n}")
    seeds_path.write_text(text, encoding="utf-8", newline="\r\n")


def _link_markets(markets_path: Path, promoted: list[dict[str, Any]]) -> None:
    """markets.json 的 source_ids 必须与种子逐市场一一对应。"""
    text = markets_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    for market in json.loads(text)["markets"]:
        market_id = market["market_id"]
        new_ids = [
            source["source_id"]
            for source in promoted
            if market_id in source["markets"] and source["source_id"] not in market["source_ids"]
        ]
        if not new_ids:
            continue
        # 各市场的末项可能相同，按 market_id 锚点定位各自的区块。
        anchor = text.index(f'"market_id": "{market_id}"')
        start = text.index('"source_ids": [', anchor)
        close = text.index("\n      ]", start)
        addition = ",\n".join(f'        "{source_id}"' for source_id in new_ids)
        text = text[:close] + ",\n" + addition + text[close:]
    markets_path.write_text(text, encoding="utf-8", newline="\r\n")


def promote(
    *,
    registry_path: Path = source_registry.REGISTRY_PATH,
    lock_path: Path | None = None,
    seeds_path: Path = source_registry.SEEDS_PATH,
    markets_path: Path = source_registry.MARKETS_PATH,
    limit: int | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """提升已复验的采集来源。返回只含计数的摘要。"""
    registry = source_registry.load_registry(registry_path)
    seeds = source_registry.load_seeds(seeds_path, markets_path=markets_path)
    chosen, skipped = selectable(registry, seeds, now=now)
    if limit is not None:
        chosen = chosen[: max(0, limit)]

    summary: dict[str, Any] = {
        "schema_version": 1,
        "registry_size": len(registry["sources"]),
        "seeds_before": len(seeds["sources"]),
        "promotable": len(chosen),
        "skipped": dict(sorted(skipped.items())),
        "applied": False,
    }
    if dry_run or not chosen:
        return summary

    _append_seeds(seeds_path, [render_seed(source) for source in chosen])
    _link_markets(markets_path, chosen)
    try:
        # 种子契约在落盘后立即复核；失败则不改注册表 origin，便于人工回退。
        promoted_seeds = source_registry.load_seeds(seeds_path, markets_path=markets_path)
    except source_registry.SourceValidationError as error:
        raise SeedPromotionError(f"promoted catalog is invalid: {error}") from error

    adopted = source_registry.adopt_sources_as_seeds(
        [source["source_id"] for source in chosen],
        registry_path=registry_path,
        lock_path=lock_path,
        now=now,
    )
    summary["applied"] = True
    summary["seeds_after"] = len(promoted_seeds["sources"])
    summary["registry_adopted"] = adopted["changed"]
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=source_registry.REGISTRY_PATH)
    parser.add_argument("--seeds", type=Path, default=source_registry.SEEDS_PATH)
    parser.add_argument("--markets", type=Path, default=source_registry.MARKETS_PATH)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        summary = promote(
            registry_path=args.registry,
            seeds_path=args.seeds,
            markets_path=args.markets,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    except (
        SeedPromotionError,
        source_registry.SourceRegistryError,
        json.JSONDecodeError,
        OSError,
    ) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, "summary": summary}, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
