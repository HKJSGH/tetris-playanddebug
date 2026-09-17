"""B07：预览必须画「下一个」，与本次生成的方块不同。"""
from __future__ import annotations


def test_b07_preview_shows_next_kind(make_game, force_pick, capture_events):
    game = make_game()
    evs = capture_events(game)
    force_pick(game, "T", "L")
    game.next_kind = "S"
    block = game._generate_new_block()
    previews = [e for e in evs if e["event"] == "preview"]
    assert previews and previews[0]["kind"] == game.next_kind
    assert previews[0]["kind"] != block["kind"]


def test_b07_spawn_chain_consistent(make_game, force_pick, capture_events):
    game = make_game()
    force_pick(game, "T", "L")
    game.next_kind = "S"
    first = game._generate_new_block()
    second = game._generate_new_block()
    assert second["kind"] == "T"
    assert first["kind"] == "S"
