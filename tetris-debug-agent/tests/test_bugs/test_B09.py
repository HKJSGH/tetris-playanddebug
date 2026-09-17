"""B09：暂停后必须能恢复；未暂停时恢复调用应为空操作。"""
from __future__ import annotations


def test_b09_resume_after_pause(make_game, capture_events):
    game = make_game()
    evs = capture_events(game)
    game.paused = True
    game._resume()
    assert game.paused is False
    assert any(e["event"] == "resume" for e in evs)


def test_b09_resume_is_noop_when_not_paused(make_game, capture_events):
    game = make_game()
    evs = capture_events(game)
    game._resume()
    assert not any(e["event"] == "resume" for e in evs)
