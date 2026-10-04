"""B09：暂停后必须能恢复；未暂停时「恢复」应是空操作。

【玩家可见现象】
按 P 暂停后再也回不去，游戏冻结在暂停画面，只能关窗口。

【注入的缺陷】
_resume 的守卫条件被取反了：
buggy 版 `if self.paused: return`（暂停时直接返回——恰恰把恢复入口堵死）；
干净版 `if not self.paused: return`（只在「没暂停」时才拒绝恢复调用）。

【测试思路】
两条用例把守卫的两个方向都钉死：
  - paused=True 时调 _resume：必须解除暂停并发出 resume 埋点事件
    （buggy 版一进来就被反逻辑挡回，paused 仍是 True，红）
  - paused=False 时调 _resume：必须是空操作、不得发出 resume 事件
    （防「把守卫整个删掉」的假修复——那样第一个用例能过，游戏逻辑却错了）
"""
from __future__ import annotations


def test_b09_resume_after_pause(make_game, capture_events):
    """暂停状态下恢复：paused 必须翻回 False，且发出 resume 事件。"""
    game = make_game()
    evs = capture_events(game)    # 截获埋点，验证游戏有没有真的发 resume 事件
    game.paused = True            # 直接置暂停态（不经过按键，聚焦 _resume 本身）
    game._resume()                # 触发恢复
    assert game.paused is False                       # 暂停被解除（buggy 版仍 True，红）
    assert any(e["event"] == "resume" for e in evs)   # 埋点事件也发出了


def test_b09_resume_is_noop_when_not_paused(make_game, capture_events):
    """对照：没暂停时调恢复必须是无害空操作（不得误发 resume 事件）。"""
    game = make_game()
    evs = capture_events(game)
    game._resume()
    assert not any(e["event"] == "resume" for e in evs)
