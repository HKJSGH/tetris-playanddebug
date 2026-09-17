r"""record_gold.py — 出题方侧 golden 快照录制（plan 第 4 步）。

headless 驱动 clean 基线跑 12 个单机制场景 SC_B01..SC_B12，同 driver 在
buggy 版上重放 diff 得 affected_bug_ids（门控 manifest），快照到沙盒
data/gold/golden.json。录制末尾每侧连跑两遍自校验一致，否则非零码退出。

driver JSON 规范（ops 顺序执行，执行器见 run_scenario / run_launch_probe）：
  {"op": "set_grid",     "spec": [[r, c, kind], ...]}
  {"op": "force_block",  "kind": "O", "cr": [5, 10]}
  {"op": "set",          "attr": "paused", "value": true}
  {"op": "force_pick",   "kinds": ["T", "L"]}
  {"op": "call",         "method": "_game_loop", "n": 1}
  {"op": "canvas_count", "tag": "preview"}          # 结果并入 final
  {"op": "module_attr",  "name": "COLORS"}          # 结果并入 final
SC_B12 特判 launch_probe：FakeGame 注入模块级 TetrisGame 驱动 launch()。

用法：
  python scripts/record_gold.py                 # 主模式：4 子进程 + 自校验 + 写盘
  python scripts/record_gold.py --stream --side clean --pkg-root DIR --runs-root DIR
                                                # worker：单遍单侧，stdout 输出 JSON
clean/buggy 分进程：两版本都是 game 包，sys.path 隔离后单进程无法同时加载。
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[1].parent          # tetris-playanddebug/
GAME_ROOT = HERE.parents[1]                 # tetris-game/
SANDBOX_ROOT = REPO_ROOT / "tetris-debug-agent"

SEED = 42
DRIVER_B02_GRID = [[11, 4, "J"], [11, 5, "L"]]

SCENARIOS: list[dict] = [
    {
        "id": "SC_B01",
        "seed": SEED,
        "driver": [
            {"op": "force_block", "kind": "O", "cr": [5, 3]},
            {"op": "call", "method": "_game_loop"},
        ],
    },
    {
        "id": "SC_B02",
        "seed": SEED,
        "driver": [
            {"op": "set_grid", "spec": DRIVER_B02_GRID},
            {"op": "force_block", "kind": "O", "cr": [5, 10]},
            {"op": "call", "method": "_on_rotate"},
        ],
    },
    {
        "id": "SC_B03",
        "seed": SEED,
        "driver": [
            {"op": "set_grid", "spec": [[19, c, "Z"] for c in range(12)]},
            {"op": "call", "method": "_check_and_clear"},
        ],
    },
    {
        "id": "SC_B04",
        "seed": SEED,
        "driver": [
            {"op": "set_grid", "spec": [[0, c, "Z"] for c in range(12)]},
            {"op": "set", "attr": "current_block", "value": None},
            {"op": "call", "method": "_game_loop"},
        ],
    },
    {
        "id": "SC_B05",
        "seed": SEED,
        "driver": [
            {"op": "force_block", "kind": "O", "cr": [10, 5]},
            {"op": "call", "method": "_on_right"},
        ],
    },
    {
        "id": "SC_B06",
        "seed": SEED,
        "driver": [
            {"op": "module_attr", "name": "COLORS"},
            {"op": "module_attr", "name": "PREVIEW_COLORS"},
        ],
    },
    {
        "id": "SC_B07",
        "seed": SEED,
        "driver": [
            {"op": "set", "attr": "next_kind", "value": "S"},
            {"op": "force_pick", "kinds": ["T", "L"]},
            {"op": "call", "method": "_generate_new_block", "n": 2},
        ],
    },
    {
        "id": "SC_B08",
        "seed": SEED,
        "driver": [
            {"op": "set_grid", "spec": [[18, c, "Z"] for c in range(12)] + [[19, c, "Z"] for c in range(12)]},
            {"op": "call", "method": "_check_and_clear"},
        ],
    },
    {
        "id": "SC_B09",
        "seed": SEED,
        "driver": [
            {"op": "set", "attr": "paused", "value": True},
            {"op": "call", "method": "_resume"},
        ],
    },
    {
        "id": "SC_B10",
        "seed": SEED,
        "driver": [
            {"op": "force_block", "kind": "I", "cr": [6, 5]},
            {"op": "call", "method": "_on_land"},
        ],
    },
    {
        "id": "SC_B11",
        "seed": SEED,
        "driver": [
            {"op": "set", "attr": "next_kind", "value": "O"},
            {"op": "call", "method": "_draw_preview", "n": 3},
            {"op": "canvas_count", "tag": "preview"},
        ],
    },
    {
        "id": "SC_B12",
        "seed": SEED,
        "driver": [{"op": "launch_probe"}],
    },
]

SCENARIO_BY_ID = {s["id"]: s for s in SCENARIOS}


# ---- 场景执行 ---------------------------------------------------------------

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


def run_scenario(mod, scenario: dict, runs_root: Path) -> dict:
    """在给定模块（tetris_v0 或 tetris_buggy）上执行一个场景。"""
    sid = scenario["id"]
    driver = scenario["driver"]
    if any(op["op"] == "launch_probe" for op in driver):
        return run_launch_probe(mod, scenario, runs_root)

    schedule_calls: list[int] = []
    mod.TetrisGame._schedule = lambda self, ms: schedule_calls.append(ms)
    mod.messagebox.askyesno = lambda *a, **k: False

    events: list[dict] = []
    game = mod.TetrisGame(round_id=1, seed=scenario["seed"], runs_root=runs_root / sid)
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
                "cell_list": [list(c) for c in mod.SHAPES[op["kind"]]],
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
            extras[f"module_{op['name']}"] = getattr(mod, op["name"])
        else:
            raise ValueError(f"unknown driver op: {kind}")

    return {
        "events": events,
        "final": _capture_final(game, extras),
        "schedule_trace": schedule_calls,
    }


def run_launch_probe(mod, scenario: dict, runs_root: Path) -> dict:
    """SC_B12：FakeGame 注入驱动 launch()，捕获跨局 initial_pos 传递。"""
    calls: list[dict] = []

    class FakeGame:
        def __init__(self, round_id=1, seed=42, runs_root=None, initial_pos=None):
            self._play_again = len(calls) == 0
            self._final_pos = "+100+80"
            calls.append({"round_id": round_id, "initial_pos": initial_pos})

        def run(self):
            pass

    orig = mod.TetrisGame
    mod.TetrisGame = FakeGame
    try:
        mod.launch(round_id=1, seed=scenario["seed"], runs_root=runs_root / scenario["id"])
    finally:
        mod.TetrisGame = orig
    return {"events": [], "final": {"launch_calls": calls}, "schedule_trace": []}


# ---- worker / 主模式 --------------------------------------------------------

def stream_worker(side: str, pkg_root: Path, runs_root: Path) -> str:
    """单遍单侧录制，stdout 输出 JSON（主模式经子进程调用）。"""
    sys.path.insert(0, str(pkg_root))
    if side == "clean":
        mod_name = "game.tetris_v0"
    elif side == "buggy":
        mod_name = "game.tetris_buggy"
    else:
        raise SystemExit(f"unknown side: {side}")
    import importlib

    mod = importlib.import_module(mod_name)

    results = {}
    for sc in SCENARIOS:
        results[sc["id"]] = run_scenario(mod, sc, runs_root)
    return json.dumps({"side": side, "results": results}, ensure_ascii=False)


def diff_summary(clean: dict, buggy: dict) -> list[str]:
    """逐场景列出差异顶层键与 final 内字段，供人工核对。"""
    lines = []
    for sid in SCENARIO_BY_ID:
        c, b = clean[sid], buggy[sid]
        diffs = []
        for key in ("events", "schedule_trace"):
            if c[key] != b[key]:
                diffs.append(key)
        for fk in c["final"]:
            if c["final"][fk] != b["final"].get(fk):
                diffs.append(f"final.{fk}")
        lines.append(f"  {sid}: {'SAME' if not diffs else ', '.join(diffs)}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", action="store_true", help="worker 模式：单遍单侧录制")
    parser.add_argument("--side", choices=["clean", "buggy"])
    parser.add_argument("--pkg-root", type=Path)
    parser.add_argument("--runs-root", type=Path)
    parser.add_argument("--out", type=Path, default=SANDBOX_ROOT / "data" / "gold" / "golden.json")
    args = parser.parse_args()

    if args.stream:
        print(stream_worker(args.side, args.pkg_root, args.runs_root))
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="record_gold_runs_"))
    pkg_roots = {"clean": GAME_ROOT, "buggy": SANDBOX_ROOT}

    def record(side: str) -> dict:
        proc = subprocess.run(
            [sys.executable, str(HERE), "--stream", "--side", side,
             "--pkg-root", str(pkg_roots[side]), "--runs-root", str(tmp)],
            capture_output=True, text=True, encoding="utf-8",
        )
        if proc.returncode != 0:
            raise SystemExit(f"{side} worker 失败:\n{proc.stderr}")
        return json.loads(proc.stdout)

    # 每侧两遍自校验（独立子进程，diff 必须为空）
    runs = {side: [record(side) for _ in range(2)] for side in ("clean", "buggy")}
    for side, pair in runs.items():
        if pair[0] != pair[1]:
            raise SystemExit(f"自校验失败：{side} 两遍录制不一致")
    clean = runs["clean"][0]["results"]
    buggy = runs["buggy"][0]["results"]

    print("场景 diff（clean vs buggy，供人工核对）：")
    print("\n".join(diff_summary(clean, buggy)))

    scenarios = []
    for sc in SCENARIOS:
        sid = sc["id"]
        c, b = clean[sid], buggy[sid]
        bug_id = sid.removeprefix("SC_")
        scenarios.append({
            "id": sid,
            "seed": sc["seed"],
            "driver": sc["driver"],
            "affected_bug_ids": [bug_id] if c != b else [],
            **c,
        })
    if all(not s["affected_bug_ids"] for s in scenarios):
        raise SystemExit("全部场景 clean==buggy：bug 未注入或场景失效")

    v0_path = GAME_ROOT / "game" / "tetris_v0.py"
    golden = {
        "format": "golden-v1",
        "recorded_from_sha256": hashlib.sha256(v0_path.read_bytes()).hexdigest(),
        "scenarios": scenarios,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(golden, ensure_ascii=False, indent=2), encoding="utf-8")

    n_aff = sum(1 for s in scenarios if s["affected_bug_ids"])
    print(f"golden: {args.out}（{len(scenarios)} 场景，{n_aff} 个 affected 非空）")
    print(f"自校验: PASS（clean/buggy 各两遍一致）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
