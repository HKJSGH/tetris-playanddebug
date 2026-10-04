"""B05：按右键应向右移动一格；左键行为不受影响。

【玩家可见现象】
按「右方向键」方块往左跑，方向完全反了。

【注入的缺陷】
_on_right 是从 _on_left 复制改造的，列增量忘了改号：
buggy 版 `self._try_move([-1, 0], action="right")`——右键却传了 -1（向左）。
修复 = 改成 [1, 0]。

【测试思路】
cr 存的是 [列, 行]，所以只看 cr[0]（列坐标）的变化：
右键必须 列+1，左键必须 列-1。两条用例成对，锁定「各自方向都正确」，
防止把右键修好时顺手改坏左键（复制粘贴型 bug 的典型连带）。
用 O 块居中摆放，四周空旷，移动不会被边界/障碍干扰。
"""
from __future__ import annotations


def test_b05_right_moves_right(make_game, force_block):
    """右键：列坐标必须 +1（修复前是 -1，即 5→4，红）。"""
    game = make_game()
    force_block(game, "O", [5, 5])    # O 块摆在第 5 列、第 5 行
    game._on_right(None)              # 按右键
    assert game.current_block["cr"][0] == 6   # 列 5 → 6


def test_b05_left_still_moves_left(make_game, force_block):
    """对照：左键仍是列 -1（5→4），右键的修复不得波及左键。"""
    game = make_game()
    force_block(game, "O", [5, 5])
    game._on_left(None)
    assert game.current_block["cr"][0] == 4
