"""B04：生成位置被占（堆到顶部）必须结束游戏。"""
from __future__ import annotations


def test_b04_game_over_when_spawn_blocked(make_game, set_grid):
    game = make_game()
    set_grid(game, [(0, c, "J") for c in range(12)])
    game.current_block = None
    game._game_loop()
    assert game.game_over is True


def test_b04_no_game_over_when_spawn_free(make_game):
    game = make_game()
    game.current_block = None
    game._game_loop()
    assert game.game_over is False
