"""B03：消除满行必须加分（每消一行 +10 分）。

【玩家可见现象】
消行后右上角分数纹丝不动，消除白消了。

【注入的缺陷】
_check_and_clear 里「更新分数」那一行被注释/删掉了：
干净版是 `self.score += cleared * 10`，buggy 版只有 lines_cleared_total
在累加，score 从不变化（见 NOTE 注释「分数更新还没接上，先跑通再说」）。

【测试思路】
把最底一行（行 19）砌满 12 格 → 调一次 _check_and_clear →
断言三件事同时成立：加了 10 分、消行计数 +1、整行被清空。
buggy 版前两条里 score==10 必然失败（分数停在 0），红。
"""
from __future__ import annotations


def test_b03_line_clear_adds_score(make_game, set_grid):
    """消一行：score +10、lines +1、该行清空，三者缺一不可。"""
    game = make_game()
    # 行 19（最底行）12 列全部砌满 —— 一个完整的可消除行
    set_grid(game, [(19, c, "J") for c in range(12)])
    game._check_and_clear()                      # 驱动一次消行检测
    assert game.score == 10                      # 每消一行加 10 分（修复前 score 仍为 0）
    assert game.lines_cleared_total == 1         # 消行计数 +1
    assert all(v == "" for v in game.block_list[19])   # 行 19 被清空
