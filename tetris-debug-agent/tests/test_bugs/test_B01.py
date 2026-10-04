"""B01：O 块下落帧间隔应与其他方块一致（FPS），不得被特殊加速。

【玩家可见现象】
O 型方块下落速度明显快于其他方块（约 5 倍速），肉眼可辨。

【注入的缺陷】
_game_loop 里给 O 块写了特殊分支：`if kind == "O": self._schedule(FPS // 5)`，
其余方块走 `self._schedule(FPS)`（FPS=200ms，见模块顶部常量）。

【测试思路】
正常注册定时器会让测试被真实下落搅局，因此基座把 `_schedule` 换成了
「记小本本」：调用参数被捕获进 after_calls 列表，游戏以为设了定时器。
于是「下落间隔」直接变成可断言的数字——调一次 _game_loop，看它登记的
间隔值是多少。
"""
from __future__ import annotations

from game.tetris_buggy import FPS


def test_b01_o_piece_normal_fall_rate(make_game, force_block, after_calls):
    """O 块走一遍主循环，登记的定时器间隔必须是 FPS（修复前是 FPS//5=40，红）。"""
    game = make_game()
    force_block(game, "O", [5, 3])   # 直接指定：当前下落的是 O 块，摆在中场
    game._game_loop()                # 驱动一次主循环（正常应在此设定时器）
    assert after_calls[-1] == FPS    # 最近一次登记的间隔 == FPS


def test_b01_other_pieces_same_rate(make_game, force_block, after_calls):
    """对照组：S 块的间隔也是 FPS。

    没有这条，「O 块 == FPS」可能靠「所有块都被改成别的值」糊弄过去；
    有了它，任何改动都必须让 O 与其他块一致才算修复。
    """
    game = make_game()
    force_block(game, "S", [5, 3])
    game._game_loop()
    assert after_calls[-1] == FPS
