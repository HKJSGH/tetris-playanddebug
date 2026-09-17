"""B01：O 块下落帧间隔应与其他方块一致（FPS）。"""
from __future__ import annotations

from game.tetris_buggy import FPS


def test_b01_o_piece_normal_fall_rate(make_game, force_block, after_calls):
    game = make_game()
    force_block(game, "O", [5, 3])
    game._game_loop()
    assert after_calls[-1] == FPS


def test_b01_other_pieces_same_rate(make_game, force_block, after_calls):
    game = make_game()
    force_block(game, "S", [5, 3])
    game._game_loop()
    assert after_calls[-1] == FPS
