"""B10：硬降（空格键）必须把方块一次性送到落点，下落距离大于 0。

【玩家可见现象】
按空格硬降，方块几乎原地不动（或只挪一格），要按住下键慢慢挪到底。

【注入的缺陷】
_on_land 用「逐格向下扫描」计算落底距离：
`for ri in range(r + 1, r + 1)`——起点和终点相同，range 为空，
循环体一次都不执行，算出的距离恒为 0（NOTE 注释「硬降的下落高度靠
扫描区间算，边界留意一下」）。干净版终点是棋盘底 R=20。

【测试思路】
用竖直的 I 块最好算：I 块四格纵向排成一列（相对行 -2..+1），
摆在 [6,5] 后占第 6 列、行 3~6，正下方 13 格全空。
_on_land 对每个格子向下数空格、取最小值作为整块的下落距离：
  - 最低格（行 6）向下数到行 19 → 距离 13
  - 其余格子更高，距离更大 → min 取 13
  - 结果：cr[1]（行）从 5 变 5+13=18，最低格落地在行 19（棋盘底）
断言两点：hard_drop 事件里的 distance > 0（buggy 版恒 0，红），且落点正确。
"""
from __future__ import annotations


def test_b10_hard_drop_reaches_bottom(make_game, force_block, capture_events):
    """硬降：距离必须 >0 且方块真的落到棋盘底（修复前距离恒 0，红）。"""
    game = make_game()
    force_block(game, "I", [6, 5])    # I 块（竖直）摆第 6 列、行 3~6
    evs = capture_events(game)        # 截获埋点：hard_drop 事件自带 distance 字段
    game._on_land(None)               # 触发硬降
    drops = [e for e in evs if e["event"] == "hard_drop"]
    assert drops and drops[0]["distance"] > 0   # buggy 版 distance=0，红
    assert game.current_block["cr"][1] == 18    # 行 5 + 下落 13 = 18，最低格落地行 19
