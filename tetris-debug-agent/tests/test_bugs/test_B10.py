"""B10：硬降必须把方块送到落点，距离大于 0。"""
from __future__ import annotations


def test_b10_hard_drop_reaches_bottom(make_game, force_block, capture_events):
    game = make_game()
    force_block(game, "I", [6, 5])
    evs = capture_events(game)
    game._on_land(None)
    drops = [e for e in evs if e["event"] == "hard_drop"]
    assert drops and drops[0]["distance"] > 0
    assert game.current_block["cr"][1] == 18
