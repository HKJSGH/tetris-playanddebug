"""headless 测试基座：不进 mainloop，直接驱动 TetrisGame 方法。

make_game 工厂在构造前完成三件事，保证无真实定时器/弹窗副作用：
1. 类级 patch ``_schedule``：捕获 ms 参数到 ``after_calls``，不注册 win.after
2. patch ``messagebox.askyesno``（屏蔽结算弹窗）
3. runs_root 指向 tmp_path

各测试通过 helper 摆局面（force_block / set_grid / force_pick / capture_events），
断言「正确行为」，因此修复前红（命中注入 bug）、修复后绿。
"""
from __future__ import annotations

import tkinter as tk

import pytest

import game.tetris_buggy as tb
from game.tetris_buggy import SHAPES, TetrisGame

_TK_MASTER: tk.Tk | None = None
_ORIG_TK = tk.Tk  # monkeypatch 会改写 tkinter.Tk，master 必须用原始引用创建


def _get_master() -> tk.Tk:
    """全进程唯一的真实 Tk root（隐藏）。

    这台机器上同进程反复创建 Tk root 约到 13 个就触发 _tkinter.TclError
    （与是否 destroy 无关）；游戏窗口统一降级为共享 master 下的 Toplevel，
    Toplevel 的创建/销毁循环无此限制。
    """
    global _TK_MASTER
    if _TK_MASTER is None or not _TK_MASTER.winfo_exists():
        _TK_MASTER = _ORIG_TK()
        _TK_MASTER.withdraw()
    return _TK_MASTER


@pytest.fixture
def after_calls(monkeypatch) -> list[int]:
    """捕获所有 _schedule(ms) 调用（B01 断言依据）。"""
    calls: list[int] = []
    monkeypatch.setattr(TetrisGame, "_schedule", lambda self, ms: calls.append(ms))
    return calls


@pytest.fixture
def make_game(tmp_path, monkeypatch, after_calls):
    """工厂 fixture：_make(**kw) -> TetrisGame，teardown 自动销毁窗口。"""
    monkeypatch.setattr(tb.messagebox, "askyesno", lambda *a, **k: False)
    monkeypatch.setattr(tb.tk, "Tk", lambda: tk.Toplevel(_get_master()))
    created = []

    def _make(**kw):
        kw.setdefault("round_id", 900 + len(created))
        kw.setdefault("seed", 42)
        game = TetrisGame(runs_root=tmp_path / "runs", **kw)
        game.win.withdraw()
        game._open_feedback_window = lambda blocking=True: None
        created.append(game)
        return game

    yield _make
    for g in created:
        try:
            g.win.destroy()
        except Exception:
            pass


@pytest.fixture
def force_block():
    """直接放置当前活动方块。"""

    def _force(game, kind: str, cr) -> None:
        game.current_block = {
            "kind": kind,
            "cell_list": [list(c) for c in SHAPES[kind]],
            "cr": list(cr),
        }

    return _force


@pytest.fixture
def set_grid():
    """批量填充已落定网格：spec = [(row, col, kind), ...]。"""

    def _set(game, spec) -> None:
        for r, c, kind in spec:
            game.block_list[r][c] = kind

    return _set


@pytest.fixture
def capture_events():
    """patch game.rec.record，返回事件捕获列表（不透传落盘）。"""
    captured: list[dict] = []

    def _capture(game) -> list[dict]:
        game.rec.record = lambda event: captured.append(dict(event))
        return captured

    return _capture


@pytest.fixture
def force_pick():
    """固定 _pick_kind 依次返回给定 kind，消除随机性。"""

    def _force(game, *kinds) -> None:
        seq = iter(kinds)
        game._pick_kind = lambda: next(seq)

    return _force
