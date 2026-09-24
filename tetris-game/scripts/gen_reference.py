r"""gen_reference.py — 出题方侧：从 clean 基线生成「正确渲染基准截图」（阶段四 A3）。

headless 驱动 tetris_v0.py（clean 版）摆出 5 个标准状态，用 ImageGrab 抓取
窗口/模块裁剪图，产出沙盒 data/reference/{board,preview,sidebar,pause_panel,
window}.png + manifest.json。agent 的 vision 节点按 manifest 读这些图做
「玩家截图 vs 基准图」对比（B6 颜色错乱、B11 预览残影等可见异常的对照面）。

Tk root 复用：真实 tk.Tk master withdraw 后，把 game.tetris_v0 里的 tk.Tk
patch 成 Toplevel(master)——同进程 5 个状态各建一个「伪 root」Toplevel，避免
多真 root 的 Tcl 崩溃；游戏窗不 withdraw（截图需要可见），固定 initial_pos。

用法：
  python scripts/gen_reference.py [--sandbox-root DIR] [--seed 42] [--force]
产物需人工检视后使用（DPI/遮挡/主题都可能导致抓图脏了，脏了删目录重跑）。
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
GAME_ROOT = HERE.parents[1]                 # tetris-game/
REPO_ROOT = GAME_ROOT.parent                # tetris-playanddebug/
DEFAULT_SANDBOX = REPO_ROOT / "tetris-debug-agent"

# ---- 5 个标准状态（driver 复用 record_gold 的 op 语义 + spawn_and_draw） ------
# captures: (crop 名, 落盘模块名, 图例 label)——每个模块名全局只产自一个状态
STATES: list[dict] = [
    {
        "id": "fresh_spawn",
        "driver": [
            {"op": "spawn_and_draw"},
        ],
        "captures": [("window", "window", "整体窗口")],
    },
    {
        "id": "i_piece_color",
        # B06 对照面：盘面 I 行与预览 I 同为青色
        "driver": [
            {"op": "set_grid", "spec": [[10, c, "I"] for c in range(3, 7)]},
            {"op": "call", "method": "_draw_board"},
            {"op": "set", "attr": "next_kind", "value": "I"},
            {"op": "call", "method": "_draw_preview"},
        ],
        "captures": [("board", "board", "游戏区（棋盘格）")],
    },
    {
        "id": "preview_overlap",
        # B11 对照面：连画 3 次，clean 版预览仍为单轮廓 4 格
        "driver": [
            {"op": "set", "attr": "next_kind", "value": "I"},
            {"op": "call", "method": "_draw_preview", "n": 3},
        ],
        "captures": [("preview", "preview", "「下一个」预览区")],
    },
    {
        "id": "stacked_board",
        "driver": [
            {"op": "set_grid", "spec": (
                [[18, c, k] for c, k in zip(range(0, 10), ["L", "J", "I", "Z", "S", "T", "O", "L", "J", "I"])]
                + [[19, c, k] for c, k in zip(range(1, 11), ["Z", "S", "T", "O", "L", "J", "I", "Z", "S", "T"])]
            )},
            {"op": "set", "attr": "score", "value": 120},
            {"op": "set", "attr": "next_kind", "value": "J"},
            {"op": "call", "method": "_draw_preview"},
            {"op": "call", "method": "_update_score_display"},
            {"op": "call", "method": "_draw_board"},
        ],
        "captures": [("sidebar", "sidebar", "右侧信息栏（分数/预览/提示）")],
    },
    {
        "id": "paused_panel",
        "driver": [
            {"op": "set", "attr": "paused", "value": True},
            {"op": "call", "method": "_open_pause_panel"},
        ],
        "captures": [("pause_panel", "pause_panel", "暂停面板")],
    },
]


def _enable_dpi_awareness() -> None:
    """Windows 下开 per-monitor DPI 感知，保证 winfo 坐标与物理像素一致。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 — 非 Windows / 已设置时忽略
        pass


def _apply_ops(mod, game, driver: list[dict]) -> None:
    """执行 driver ops（set_grid / set / force_block / force_pick / call / spawn_and_draw）。"""
    for op in driver:
        kind = op["op"]
        if kind == "set_grid":
            for r, c, k in op["spec"]:
                game.block_list[r][c] = k
        elif kind == "set":
            setattr(game, op["attr"], op["value"])
        elif kind == "force_block":
            game.current_block = {
                "kind": op["kind"],
                "cell_list": [list(c) for c in mod.SHAPES[op["kind"]]],
                "cr": list(op["cr"]),
            }
        elif kind == "force_pick":
            seq = iter(op["kinds"])
            game._pick_kind = lambda: next(seq)  # noqa: B023
        elif kind == "call":
            fn = getattr(game, op["method"])
            for _ in range(op.get("n", 1)):
                fn()
        elif kind == "spawn_and_draw":
            block = game._generate_new_block()
            game._draw_cells(block["cr"][0], block["cr"][1], block["cell_list"],
                             game._color_for(block["kind"]))
        else:
            raise ValueError(f"unknown driver op: {kind}")


def _grab(bbox: tuple[int, int, int, int], out: Path) -> None:
    """按屏幕绝对坐标抓图（all_screens 覆盖多显示器负坐标）。"""
    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    img.save(out)


def build_state(mod, tk, master, state: dict, runs_root: Path, out_dir: Path) -> list[dict]:
    """跑一个标准状态并按 captures 抓图，返回 manifest modules 条目。"""
    mod.TetrisGame._schedule = lambda self, ms: None          # 不注册真定时器
    mod.messagebox.askyesno = lambda *a, **k: False

    game = mod.TetrisGame(round_id=0, seed=state["seed"],
                          runs_root=runs_root / state["id"], initial_pos="+100+80")
    game._open_feedback_window = lambda blocking=True: None
    game.rec.record = lambda e: None                          # 基准录制不落遥测

    _apply_ops(mod, game, state["driver"])

    win = game.win
    win.attributes("-topmost", True)
    win.update_idletasks()
    win.update()
    time.sleep(0.2)                                           # 等合成器把帧画完

    x, y = win.winfo_rootx(), win.winfo_rooty()
    w, h = win.winfo_width(), win.winfo_height()
    board_w = mod.C * mod.cell_size

    crops = {
        "window": (x, y, x + w, y + h),
        "board": (x, y, x + board_w, y + h),
        "sidebar": (x + board_w, y, x + w, y + h),
        "preview": (x + board_w, y + 100, x + w, y + 200),    # 「下一个」标题 y=110、预览格 y≈130-190
    }
    if game._pause_panel is not None:
        p = game._pause_panel
        p.attributes("-topmost", True)
        p.update_idletasks()
        p.update()
        time.sleep(0.2)
        crops["pause_panel"] = (p.winfo_rootx(), p.winfo_rooty(),
                                p.winfo_rootx() + p.winfo_width(),
                                p.winfo_rooty() + p.winfo_height())

    entries = []
    for crop, name, label in state["captures"]:
        if crop not in crops:
            raise SystemExit(f"state {state['id']}: unknown crop {crop!r}")
        bbox = crops[crop]
        out = out_dir / f"{name}.png"
        _grab(bbox, out)
        entries.append({"name": name, "file": out.name, "label": label,
                        "bbox": list(bbox), "state": state["id"]})

    if game._pause_panel is not None:
        game._pause_panel.destroy()
    win.destroy()
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-root", type=Path, default=DEFAULT_SANDBOX,
                        help="agent 沙盒根目录（产物写入 <root>/data/reference/）")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 reference 目录")
    args = parser.parse_args()

    out_dir = args.sandbox_root / "data" / "reference"
    if (out_dir / "manifest.json").exists() and not args.force:
        print(f"reference 已存在（{out_dir}），确认基准图脏了再重跑：--force 覆盖")
        return 1
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    _enable_dpi_awareness()

    import tkinter as tk
    master = tk.Tk()
    master.withdraw()                     # 真 root 只作容器；游戏窗不 withdraw（要可见）
    orig_tk = tk.Tk
    tk.Tk = lambda: tk.Toplevel(master)   # TetrisGame 的 tk.Tk() → 复用 master 的 Toplevel

    sys.path.insert(0, str(GAME_ROOT))
    import game.tetris_v0 as mod  # noqa: E402

    runs_root = Path(tempfile.mkdtemp(prefix="gen_reference_runs_"))
    modules: list[dict] = []
    try:
        for state in STATES:
            state = {**state, "seed": args.seed}
            entries = build_state(mod, tk, master, state, runs_root, out_dir)
            modules.extend(entries)
            print(f"  {state['id']}: " + ", ".join(f"{e['name']}.png" for e in entries))
    finally:
        tk.Tk = orig_tk
        master.destroy()

    v0_path = GAME_ROOT / "game" / "tetris_v0.py"
    manifest = {
        "format": "reference-v1",
        "source_sha256": hashlib.sha256(v0_path.read_bytes()).hexdigest(),
        "modules": modules,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"manifest → {out_dir / 'manifest.json'}（{len(modules)} 个模块图）")
    print("请人工检视 5 张 PNG（颜色/残影/裁剪范围），确认无误后 vision 才会启用对比模式。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
