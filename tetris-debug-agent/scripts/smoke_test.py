"""无头冒烟测试：创建游戏、驱动 N tick、自动收尾，验证 6 文件落盘。

不调用 mainloop，手动调用 _game_loop 与 _on_* 模拟输入。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import game.tetris_buggy as ui_mod
from game.tetris_buggy import TetrisGame
from game.telemetry import RUNS_ROOT


def main() -> int:
    """跑冒烟流程并打印落盘文件清单与摘要。"""
    # 屏蔽结算弹窗
    ui_mod.messagebox.askyesno = lambda *a, **k: False

    runs_root = RUNS_ROOT / "smoke"
    runs_root.mkdir(parents=True, exist_ok=True)
    game = TetrisGame(round_id=997, seed=42, runs_root=runs_root)

    # 模拟 40 tick + 几次输入
    for i in range(40):
        if game.game_over:
            break
        game._game_loop()
        if i == 5:
            game._on_left(None)
        if i == 12:
            game._on_rotate(None)
        if i == 20:
            game._on_right(None)
        if i == 30:
            game._on_land(None)

    # 强制收尾（跳过反馈窗口，直接落盘）
    if not game._finalized:
        game._do_finalize("smoke_test")

    run_dir = runs_root / "round_997"
    print("run_dir:", run_dir)
    files = sorted(p.name for p in run_dir.iterdir())
    print("files:", files)
    missing = [n for n in ["meta.json", "game_state.json", "telemetry.jsonl", "errors.log", "feedback_text.md"]
               if n not in files]
    if missing:
        print("缺少文件:", missing)
        return 1
    # 校验事件带 ts 时间戳
    lines = (run_dir / "telemetry.jsonl").read_text(encoding="utf-8").strip().splitlines()
    ok_ts = all('"ts"' in ln for ln in lines)
    print(f"telemetry 事件数: {len(lines)}, 均含 ts: {ok_ts}")
    try:
        game.win.destroy()
    except Exception:
        pass
    return 0 if ok_ts else 1


if __name__ == "__main__":
    sys.exit(main())