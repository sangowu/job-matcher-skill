"""The precise stage's JD reads are shared between a round's markets."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from precise_pick import PrecisePickError, pick  # noqa: E402

CONFIG = {"top_n": 15, "precise_buffer": 5}


def _row(record_id, market, score, recommendation="low_priority"):
    markets = market if isinstance(market, list) else [market]
    return {"record_id": record_id, "market_ids": markets, "coarse_score": score,
            "recommendation": recommendation}


def _live_round():
    """The coarse scores of the 2026-10-14 Singapore/Madrid/Milan round."""
    rows = [_row(f"sg-ai-{n}", "sg", 71) for n in range(12)]
    rows += [_row("sg-flintex", "sg", 74), _row("sg-openai-1", "sg", 68), _row("sg-openai-2", "sg", 68),
             _row("sg-openai-3", "sg", 68), _row("sg-unison", "sg", 67.25), _row("sg-stripe", "sg", 67.25),
             _row("sg-fireworks", "sg", 67), _row("sg-regask", "sg", 65.75), _row("sg-razer", "sg", 53.5, "skip")]
    rows += [_row("es-elastic", "es", 68.25), _row("es-celonis-ai", "es", 64),
             _row("es-n26-1", "es", 56.25), _row("es-n26-2", ["de", "es"], 56.25), _row("es-monzo", "es", 51.75)]
    rows += [_row("it-antal", "it", 75.5), _row("it-onyx", "it", 63.5), _row("it-easynet", "it", 62.5),
             _row("it-oneteam", "it", 56), _row("it-java", "it", 45.25), _row("it-embedded", "it", 36.5)]
    return rows


def test_the_live_round_no_longer_spends_its_jd_reads_on_one_market():
    """A global top gave Singapore 18 of the 20 reads; Spain and Italy were
    reported scored from their titles alone."""
    payload = {"target_markets": ["sg", "es", "it"], "candidates": _live_round()}

    global_top = sorted(payload["candidates"], key=lambda r: -r["coarse_score"])[:20]
    assert sum(r["market_ids"] == ["sg"] for r in global_top) == 18

    result = pick(payload, CONFIG)

    assert result["share_per_market"] == 7
    # Spain and Italy have fewer than seven, so all of theirs are read and
    # Singapore takes its seven plus the two slots they leave.
    assert result["picked_by_market"] == {"sg": 9, "es": 5, "it": 6}
    assert len(result["picked"]) == 20
    assert "sg-razer" not in result["picked"]


def test_a_market_short_of_its_share_leaves_its_slots_to_the_best_of_the_rest():
    payload = {"target_markets": ["sg", "it"], "limit": 6,
               "candidates": [_row(f"sg{n}", "sg", 90 - n) for n in range(6)] + [_row("it0", "it", 40)]}

    result = pick(payload, CONFIG)

    assert result["picked_by_market"] == {"sg": 5, "it": 1}
    assert result["picked"][-1] == "it0"


def test_a_skip_never_takes_a_reserved_slot():
    payload = {"target_markets": ["sg", "it"], "limit": 2,
               "candidates": [_row("sg0", "sg", 80), _row("sg1", "sg", 70), _row("it0", "it", 75, "skip")]}

    assert pick(payload, CONFIG)["picked"] == ["sg0", "sg1"]


def test_one_market_is_a_plain_top_by_score():
    payload = {"target_markets": ["sg"], "limit": 3,
               "candidates": [_row(f"sg{n}", "sg", n) for n in range(10)]}

    result = pick(payload, CONFIG)

    assert result["share_per_market"] is None
    assert result["picked"] == ["sg9", "sg8", "sg7"]


def test_a_posting_in_several_markets_counts_once_for_the_first_of_the_round():
    payload = {"target_markets": ["es", "de"], "limit": 2,
               "candidates": [_row("both", ["de", "es"], 50), _row("de0", "de", 90), _row("de1", "de", 80)]}

    result = pick(payload, CONFIG)

    assert result["candidates_by_market"] == {"es": 1, "de": 2}
    assert set(result["picked"]) == {"both", "de0"}


def test_the_limit_defaults_to_top_n_plus_the_buffer():
    payload = {"target_markets": [], "candidates": [_row(f"x{n}", "sg", n) for n in range(30)]}

    assert len(pick(payload, CONFIG)["picked"]) == 20


@pytest.mark.parametrize(
    "payload",
    [
        {"candidates": [{"record_id": "a", "coarse_score": "high"}]},
        {"candidates": [_row("a", "sg", 1), _row("a", "sg", 2)]},
        {"candidates": "a"},
        {"limit": -1, "candidates": []},
    ],
)
def test_malformed_input_is_refused(payload):
    with pytest.raises(PrecisePickError):
        pick(payload, CONFIG)


def test_cli_reads_stdin_and_reports_errors_as_json():
    ok = subprocess.run([sys.executable, str(SCRIPTS_DIR / "precise_pick.py")],
                        input=json.dumps({"limit": 1, "candidates": [_row("a", "sg", 1)]}),
                        text=True, capture_output=True, check=False)
    bad = subprocess.run([sys.executable, str(SCRIPTS_DIR / "precise_pick.py")],
                         input="[]", text=True, capture_output=True, check=False)

    assert json.loads(ok.stdout)["picked"] == ["a"]
    assert bad.returncode == 1 and json.loads(bad.stdout)["ok"] is False
