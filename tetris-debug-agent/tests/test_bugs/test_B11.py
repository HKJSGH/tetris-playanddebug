"""B11：预览区重绘必须先清除旧预览，不得残影叠加。"""
from __future__ import annotations


def test_b11_preview_no_ghosting(make_game):
    game = make_game()
    for _ in range(3):
        game._draw_preview()
        assert len(game.canvas.find_withtag("preview")) == 4
