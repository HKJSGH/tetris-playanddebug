"""B04：新方块生成位置被占（堆到顶部）必须立即结束游戏。

【玩家可见现象】
方块堆到屏幕顶后，新方块直接「生成在死块堆里」，游戏卡死不结束。

【注入的缺陷】
_generate_new_block 缺少「生成即碰撞」判定：干净版在生成后发现新方块
与已有方块重叠就置 game_over=True；buggy 版照常生成，游戏永不结束。

【测试思路】
把第 0 行（最顶行）砌满 → 把当前方块清空（模拟上一块刚锁定）→
驱动一次 _game_loop（其中会尝试生成新方块）→ 断言 game_over。
两条用例互为对照：顶部被占必须死，顶部空着必须不死——
防止「无脑把 game_over 恒置 True」式的假修复。
"""
from __future__ import annotations


def test_b04_game_over_when_spawn_blocked(make_game, set_grid):
    """生成位置被占 → game_over 必须为 True（修复前永远 False，红）。"""
    game = make_game()
    set_grid(game, [(0, c, "J") for c in range(12)])   # 第 0 行砌满：新方块无处生成
    game.current_block = None                          # 清空当前块，逼 _game_loop 去生成新块
    game._game_loop()                                  # 主循环：生成新方块 → 撞 → 应结束
    assert game.game_over is True


def test_b04_no_game_over_when_spawn_free(make_game):
    """对照：空棋盘正常生成，不得误判结束。"""
    game = make_game()
    game.current_block = None
    game._game_loop()
    assert game.game_over is False
