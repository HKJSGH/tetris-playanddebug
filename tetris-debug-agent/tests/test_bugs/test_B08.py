"""B08：多行同时满必须全部消除。"""
from __future__ import annotations


def test_b08_multi_full_rows_all_cleared(make_game, set_grid):
    game = make_game()
    rows = [(19, c, "J") for c in range(12)] + [(18, c, "L") for c in range(12)]
    set_grid(game, rows)
    game._check_and_clear()
    assert game.lines_cleared_total == 2
    assert all(v == "" for v in game.block_list[18] + game.block_list[19])


def test_b08_single_row_still_works(make_game, set_grid):
    game = make_game()
    set_grid(game, [(19, c, "J") for c in range(12)])
    game._check_and_clear()
    assert game.lines_cleared_total == 1
