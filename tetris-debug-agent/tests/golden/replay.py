r"""golden 等价回归执行器：按 golden.json 的 driver JSON 回放当前 tetris_buggy。

driver ops 规范与 tetris-game/scripts/record_gold.py 的 run_scenario /
run_launch_probe 保持同步（format golden-v1）；本文件自包含实现，
不 import 出题方目录。

manifest 门控在 test_golden_equiv.py 中实现：仅当场景的 affected_bug_ids
⊆ 已修复 bug（data/fixes.json）∪ GOLDEN_ALLOWED_PH 环境变量放行项时，
才要求回放结果与 golden 快照一致。
"""
from __future__ import annotations

import inspect
import tempfile
import tkinter as tk
from pathlib import Path

SANDBOX_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = SANDBOX_ROOT / "data" / "gold" / "golden.json"

_TK_MASTER: tk.Tk | None = None
_ORIG_TK = tk.Tk


def _get_master() -> tk.Tk:
    """全进程唯一的真实 Tk root（原因同 tests/test_bugs/conftest.py）。"""
    global _TK_MASTER
    if _TK_MASTER is None or not _TK_MASTER.winfo_exists():
        _TK_MASTER = _ORIG_TK()
        _TK_MASTER.withdraw()
    return _TK_MASTER


def _capture_final(game, extras: dict) -> dict:
    cb = game.current_block
    final = {
        "game_over": game.game_over,
        "paused": game.paused,
        "score": game.score,
        "lines": game.lines_cleared_total,
        "next_kind": game.next_kind,
        "block_list": [row[:] for row in game.block_list],
        "current_block": None
        if cb is None
        else {
            "kind": cb["kind"],
            "cell_list": [list(c) for c in cb["cell_list"]],
            "cr": list(cb["cr"]),
        },
    }
    final.update(extras)
    return final


def replay_scenario(scenario: dict, runs_root: Path | None = None) -> dict:
    """在当前 tetris_buggy 上回放一个场景，返回 {events, final, schedule_trace}。"""
    import game.tetris_buggy as tb

    driver = scenario["driver"]
    if any(op["op"] == "launch_probe" for op in driver):
        return _replay_launch_probe(tb, scenario)

    own_root = runs_root is None
    runs_root = runs_root or Path(tempfile.mkdtemp(prefix="golden_replay_"))

    schedule_calls: list[int] = []
    orig_schedule = tb.TetrisGame._schedule
    orig_askyesno = tb.messagebox.askyesno
    orig_tk = tb.tk.Tk
    tb.TetrisGame._schedule = lambda self, ms: schedule_calls.append(ms)
    tb.messagebox.askyesno = lambda *a, **k: False
    tb.tk.Tk = lambda: tk.Toplevel(_get_master())
    try:
        events: list[dict] = []
        game = tb.TetrisGame(round_id=1, seed=scenario["seed"], runs_root=runs_root / scenario["id"])
        game.win.withdraw()
        game._open_feedback_window = lambda blocking=True: None
        game.rec.record = lambda e: events.append({k: v for k, v in e.items() if k != "ts"})

        extras: dict = {}
        for op in driver:
            kind = op["op"]
            if kind == "set_grid":
                for r, c, k in op["spec"]:
                    game.block_list[r][c] = k
            elif kind == "force_block":
                game.current_block = {
                    "kind": op["kind"],
                    "cell_list": [list(c) for c in tb.SHAPES[op["kind"]]],
                    "cr": list(op["cr"]),
                }
            elif kind == "set":
                setattr(game, op["attr"], op["value"])
            elif kind == "force_pick":
                seq = iter(op["kinds"])
                game._pick_kind = lambda: next(seq)
            elif kind == "call":
                fn = getattr(game, op["method"])
                args = [None] if len(inspect.signature(fn).parameters) > 0 else []
                for _ in range(op.get("n", 1)):
                    fn(*args)
            elif kind == "canvas_count":
                extras[f"canvas_count_{op['tag']}"] = len(game.canvas.find_withtag(op["tag"]))
            elif kind == "module_attr":
                extras[f"module_{op['name']}"] = getattr(tb, op["name"])
            else:
                raise ValueError(f"unknown driver op: {kind}")

        return {
            "events": events,
            "final": _capture_final(game, extras),
            "schedule_trace": schedule_calls,
        }
    finally:
        tb.TetrisGame._schedule = orig_schedule
        tb.messagebox.askyesno = orig_askyesno
        tb.tk.Tk = orig_tk
        if own_root:
            pass  # runs 目录留在系统临时目录，随系统清理


def _replay_launch_probe(tb, scenario: dict) -> dict:
    calls: list[dict] = []

    class FakeGame:
        def __init__(self, round_id=1, seed=42, runs_root=None, initial_pos=None):
            self._play_again = len(calls) == 0
            self._final_pos = "+100+80"
            calls.append({"round_id": round_id, "initial_pos": initial_pos})

        def run(self):
            pass

    orig = tb.TetrisGame
    tb.TetrisGame = FakeGame
    try:
        tb.launch(round_id=1, seed=scenario["seed"], runs_root=Path(tempfile.mkdtemp(prefix="golden_launch_")))
    finally:
        tb.TetrisGame = orig
    return {"events": [], "final": {"launch_calls": calls}, "schedule_trace": []}
