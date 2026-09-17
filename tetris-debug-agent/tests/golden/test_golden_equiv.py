r"""golden 等价回归：受影响 bug 已修复的场景必须与 clean 行为快照一致。

门控（解 12 bug 未修完前的死锁）：场景 affected_bug_ids ⊆ 已修复
（data/fixes.json 的 fixed_phenomena 经 truth_map 映射 ∪ GOLDEN_ALLOWED_PH
环境变量放行项）才要求通过，否则 skip。affected 为空的场景恒要求通过
（任何修复都不应破坏 clean 等价行为）。

truth_map.json 仅被本测试代码（纯逻辑门控）读取，绝不进任何 LLM prompt。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.golden.replay import GOLDEN_PATH, replay_scenario

SANDBOX_ROOT = Path(__file__).resolve().parents[2]
TRUTH_PATH = SANDBOX_ROOT / "data" / "gold" / "truth_map.json"
FIXES_PATH = SANDBOX_ROOT / "data" / "fixes.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


if not GOLDEN_PATH.exists():
    pytest.fail(f"golden 快照不存在（先在 tetris-game 侧跑 scripts/record_gold.py）: {GOLDEN_PATH}")

_SCENARIOS = _load(GOLDEN_PATH)["scenarios"]


def _allowed_bug_ids() -> set[str]:
    truth = _load(TRUTH_PATH)
    fixed_ph: set[str] = set()
    if FIXES_PATH.exists():
        fixes = _load(FIXES_PATH)
        fixed_ph = {e["phenomenon_id"] for e in fixes.get("fixed_phenomena", [])}
    extra_ph = {p.strip() for p in os.environ.get("GOLDEN_ALLOWED_PH", "").split(",") if p.strip()}
    return {truth[ph] for ph in (fixed_ph | extra_ph) if ph in truth}


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[s["id"] for s in _SCENARIOS])
def test_golden_equivalence(scenario):
    affected = set(scenario["affected_bug_ids"])
    not_fixed = affected - _allowed_bug_ids()
    if not_fixed:
        pytest.skip(f"受未修复 bug 影响: {sorted(not_fixed)}")
    actual = replay_scenario(scenario)
    expected = {k: scenario[k] for k in ("events", "final", "schedule_trace")}
    assert actual == expected, f"{scenario['id']} 与 golden 快照不一致"
