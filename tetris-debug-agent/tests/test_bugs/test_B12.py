"""B12：跨局重开必须保留上一局的窗口位置。

【玩家可见现象】
结算后选「再来一局」，新窗口总是弹回屏幕默认位置，上一局拖好的位置丢了。

【注入的缺陷】
launch 的循环里 `pos: str | None = None` 写在了循环体**内部**：
每轮循环开头都把 pos 重置为 None，上一局记录的 _final_pos 白存了。
干净版 pos 在循环外初始化一次，跨局传递（NOTE 注释「窗口位置跨局记住的
功能刚加，pos 初始化放哪要留意」）。

【测试思路 —— 换掉 TetrisGame 本体，只测 launch 的循环逻辑】
这里不造真游戏窗口：用 FakeGame 冒充 TetrisGame（monkeypatch 替换），
它只干三件事——记录自己收到的构造参数、报出预设的 _final_pos、
第 1 局说「再来一局」第 2 局说「不玩了」，从而精确驱动循环跑两圈。
断言：第 1 局 initial_pos 是 None（第一次没有历史位置，正常），
第 2 局必须收到第 1 局的 "+100+80"（buggy 版循环内重置成 None，红）。
"""
from __future__ import annotations

import game.tetris_buggy as tb


def test_b12_second_round_keeps_window_pos(tmp_path, monkeypatch):
    """两局连玩：第 2 局的 initial_pos 必须等于第 1 局的 _final_pos。"""
    calls = []   # 记录每次构造 TetrisGame 收到的参数

    class FakeGame:
        _final_pos = "+100+80"   # 冒充「上一局窗口位置」的固定返回值

        def __init__(self, round_id=1, seed=42, runs_root=None, initial_pos=None):
            self.round_id = round_id
            self.initial_pos = initial_pos
            calls.append({"round_id": round_id, "initial_pos": initial_pos})
            # 第 1 局选「再来一局」继续循环；第 2 局选「不玩了」退出
            self._play_again = len(calls) == 1

        def run(self):
            pass   # 不真的运行游戏主循环

    # 把 launch 眼里的 TetrisGame 换成 FakeGame——隔离被测对象，只测循环
    monkeypatch.setattr(tb, "TetrisGame", FakeGame)
    tb.launch(round_id=1, seed=42, runs_root=tmp_path / "runs")
    # 第 1 局无历史位置(None)；第 2 局必须继承第 1 局位置(修复前是 None，红)
    assert [c["initial_pos"] for c in calls] == [None, "+100+80"]
    assert [c["round_id"] for c in calls] == [1, 2]   # 局号也要正确递增
