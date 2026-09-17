"""B02：旋转必须做碰撞检查，旋转不得穿入已占格。"""
from __future__ import annotations

C, R = 12, 20


def _cells(game) -> set:
    cc, cr = game.current_block["cr"]
    return {(c + cc, r + cr) for c, r in game.current_block["cell_list"]}


def test_b02_rotation_rejected_when_colliding(make_game, force_block, set_grid):
    game = make_game()
    set_grid(game, [(11, 4, "J"), (11, 5, "L")])
    force_block(game, "O", [5, 10])
    game._on_rotate(None)
    occupied = {(c, r) for r in range(R) for c in range(C) if game.block_list[r][c]}
    assert not (_cells(game) & occupied)


def test_b02_rotation_applies_when_free(make_game, force_block):
    game = make_game()
    force_block(game, "O", [5, 10])
    before = [list(c) for c in game.current_block["cell_list"]]
    game._on_rotate(None)
    assert [list(c) for c in game.current_block["cell_list"]] != before
