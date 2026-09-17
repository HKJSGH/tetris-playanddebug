"""B05：右键应向右移动一格，左键不受影响。"""
from __future__ import annotations


def test_b05_right_moves_right(make_game, force_block):
    game = make_game()
    force_block(game, "O", [5, 5])
    game._on_right(None)
    assert game.current_block["cr"][0] == 6


def test_b05_left_still_moves_left(make_game, force_block):
    game = make_game()
    force_block(game, "O", [5, 5])
    game._on_left(None)
    assert game.current_block["cr"][0] == 4
