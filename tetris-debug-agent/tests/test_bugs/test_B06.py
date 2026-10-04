"""B06：游戏区与预览区色表必须一致，I 块应渲染为青色。

【玩家可见现象】
游戏区里 I 型方块是红色的，预览区里的 I 块却是青色——同一方块两种颜色。

【注入的缺陷】
两个色表被人改得不一致：COLORS["I"]="red"（游戏区用），
PREVIEW_COLORS["I"]="Cyan"（预览区用）。干净版两边都是青色。
注意 PREVIEW_COLORS 里是 "Cyan"（大写 C）——断言统一 .lower() 后比较，
只看颜色值本身，不纠结大小写写法。

【测试思路 —— 视觉 bug 不截图，查 canvas 的「对象账本」】
Tkinter Canvas 是保留模式场景图：每次 create_rectangle 都在画布内部
登记一个带标签(tag)的对象，渲染时就是照着账本画。所以：
  - 第 1 层：两个色表逐项一致（数据源头就错了，渲染必然错）
  - 第 2 层：真调一次绘制，再从账本读回每个格子的 fill 属性——
    itemcget(item, "fill") 返回的就是 Tk 真正会刷到屏幕上的颜色值，
    窗口隐藏、不开 mainloop 都不影响判断。
"""
from __future__ import annotations

from game.tetris_buggy import COLORS, PREVIEW_COLORS


def test_b06_two_color_tables_agree():
    """色表层：每个方块的两种色表取值必须一致（修复前 I 块 red vs Cyan，红）。"""
    assert COLORS["I"].lower() == PREVIEW_COLORS["I"].lower()


def test_b06_i_piece_renders_cyan(make_game, force_block):
    """渲染层：I 块画出来，账本里每个 falling 格子的填充色都应是 cyan。"""
    game = make_game()
    force_block(game, "I", [6, 5])              # I 块（竖条）摆在第 6 列、第 5 行
    game._draw_block_move(game.current_block)   # 真正执行一次绘制
    # 从 canvas 账本读回所有标记为 "falling"（当前下落块）的格子的填充色
    fills = [
        game.canvas.itemcget(i, "fill").lower()
        for i in game.canvas.find_withtag("falling")
    ]
    assert fills and all(f == "cyan" for f in fills)   # 修复前是 red，红
