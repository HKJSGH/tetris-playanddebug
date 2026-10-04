"""B07：预览区必须显示「下一个方块」，与本次生成的不同。

【玩家可见现象】
右上角「下一个」预览显示的方块，和接下来实际掉下来的那块一模一样——
预告失去了意义。

【注入的缺陷】
_generate_new_block 里两步的顺序被调换了：
buggy 版先 `self._draw_preview()` 再 `self.next_kind = self._pick_kind()`，
于是预览画的是**旧的** next_kind（也就是本次正在生成的这块）；
干净版先取新的 next_kind 再画预览。

【测试思路 —— 用事件流断言，而不是查画布】
「预览显示的是谁」是个时序问题，直接查 canvas 反而绕。基座提供了
capture_events：把游戏的埋点记录器换成「记小本本」，游戏自己发的
preview 事件里带着 kind 字段，谁画上去的一查便知。
force_pick 把随机抽取固定成序列（T, L），消除随机性，让断言可复现。
"""
from __future__ import annotations


def test_b07_preview_shows_next_kind(make_game, force_pick, capture_events):
    """preview 事件的 kind 必须等于「下一个」（修复前等于本次生成的，红）。"""
    game = make_game()
    evs = capture_events(game)        # 截获埋点：游戏发出的事件都进 evs
    force_pick(game, "T", "L")        # 固定随机序列：本次抽 T，下一个抽 L
    game.next_kind = "S"              # 手动设一个旧值，制造清晰可辨的时序
    block = game._generate_new_block()   # 生成方块：本块=S，画预览，更新 next_kind
    previews = [e for e in evs if e["event"] == "preview"]
    # 正确时序：先 next_kind=_pick_kind()=T，再画预览 → 预览显示 T
    assert previews and previews[0]["kind"] == game.next_kind
    # 且预览 ≠ 本次生成的方块（S）——这是「预告」的语义本身
    assert previews[0]["kind"] != block["kind"]


def test_b07_spawn_chain_consistent(make_game, force_pick, capture_events):
    """连续生成两次：第 1 块是旧的 next_kind(S)，第 2 块是序列里的下一个(T)。"""
    game = make_game()
    force_pick(game, "T", "L")
    game.next_kind = "S"
    first = game._generate_new_block()
    second = game._generate_new_block()
    assert second["kind"] == "T"      # 序列第 1 个 T 被第二次生成消费
    assert first["kind"] == "S"       # 首块用旧值 S（next_kind 的语义）
