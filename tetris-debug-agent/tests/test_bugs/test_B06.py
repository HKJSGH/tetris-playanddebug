"""B06：游戏区与预览区色表必须一致，I 块渲染为青色。"""
from __future__ import annotations

from game.tetris_buggy import COLORS, PREVIEW_COLORS


def test_b06_two_color_tables_agree():
    assert COLORS["I"].lower() == PREVIEW_COLORS["I"].lower()


def test_b06_i_piece_renders_cyan(make_game, force_block):
    game = make_game()
    force_block(game, "I", [6, 5])
    game._draw_block_move(game.current_block)
    fills = [
        game.canvas.itemcget(i, "fill").lower()
        for i in game.canvas.find_withtag("falling")
    ]
    assert fills and all(f == "cyan" for f in fills)
