"""B12：跨局重开必须保留上一局窗口位置。"""
from __future__ import annotations

import game.tetris_buggy as tb


def test_b12_second_round_keeps_window_pos(tmp_path, monkeypatch):
    calls = []

    class FakeGame:
        _final_pos = "+100+80"

        def __init__(self, round_id=1, seed=42, runs_root=None, initial_pos=None):
            self.round_id = round_id
            self.initial_pos = initial_pos
            calls.append({"round_id": round_id, "initial_pos": initial_pos})
            self._play_again = len(calls) == 1

        def run(self):
            pass

    monkeypatch.setattr(tb, "TetrisGame", FakeGame)
    tb.launch(round_id=1, seed=42, runs_root=tmp_path / "runs")
    assert [c["initial_pos"] for c in calls] == [None, "+100+80"]
    assert [c["round_id"] for c in calls] == [1, 2]
