"""B08：多行同时满了必须全部消除，不得只消一行。

【玩家可见现象】
一次同时消掉两行（甚至更多）时，只有最底下一行消失，上面那行原封不动。

【注入的缺陷】
_check_and_clear 的扫描循环里留了个调试用的提前退出：
buggy 版找到一个满行、处理完就 `break`（NOTE 注释「循环里的提前退出是
调试用的，先留着」），所以一次调用最多只消一行；干净版扫完整个棋盘。

【测试思路】
把行 18 和行 19 连续两行都砌满 → 调一次 _check_and_clear →
断言消行计数为 2 且两行都变空。buggy 版 break 后只消了 1 行，红。
第二条对照：单行场景必须照常工作——修复（去掉 break）不得影响单行。
"""
from __future__ import annotations


def test_b08_multi_full_rows_all_cleared(make_game, set_grid):
    """两行同时满：一次调用必须全消（修复前只消一行，红）。"""
    game = make_game()
    # 行 19 与行 18 各砌满 12 格 —— 相邻两行同时满
    rows = [(19, c, "J") for c in range(12)] + [(18, c, "L") for c in range(12)]
    set_grid(game, rows)
    game._check_and_clear()
    assert game.lines_cleared_total == 2    # buggy 版只会 +1（break 提前退出）
    # 两行都必须被清空（上方行下落后行 18/19 均为空）
    assert all(v == "" for v in game.block_list[18] + game.block_list[19])


def test_b08_single_row_still_works(make_game, set_grid):
    """对照：单行满时消 1 行——去掉 break 不得改变单行行为。"""
    game = make_game()
    set_grid(game, [(19, c, "J") for c in range(12)])
    game._check_and_clear()
    assert game.lines_cleared_total == 1
