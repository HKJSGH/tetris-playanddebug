"""B03：消除满行必须加分。"""
from __future__ import annotations


def test_b03_line_clear_adds_score(make_game, set_grid):
    game = make_game()
    set_grid(game, [(19, c, "J") for c in range(12)])
    game._check_and_clear()
    assert game.score == 10
    assert game.lines_cleared_total == 1
    assert all(v == "" for v in game.block_list[19])
