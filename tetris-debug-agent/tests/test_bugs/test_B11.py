"""B11：预览区重绘必须先清除旧预览，不得残影叠加。

【玩家可见现象】
右上角「下一个」预览区每刷新一次就多叠一层方块印子，最后糊成彩色鬼影。

【注入的缺陷】
_draw_preview 画新预览前忘了 `canvas.delete(self._preview_tag)`（销旧账），
旧预览的图形对象一直留在画布上越叠越多。

【测试思路 —— 视觉 bug 不截图，查 canvas 的「对象账本」】
Tkinter Canvas 是保留模式场景图：每次 create_rectangle 都在画布内部
登记一个带 tag 的对象，渲染时照着账本画——账本里有几条记录，屏幕上
就有几个图形；不销账，记录永远躺着，残影就一直在。
于是「有没有残影」被无损翻译成「账本上有几条 preview 记录」：
  一个方块 = 4 个格子 = 4 条记录
  正确版：每次先销旧账 → 不管刷多少次，账上恒为 4 条
  buggy 版：4 → 8 → 12 递增 → 断言炸，红
连续画 3 次是为了让 buggy 版在第 2 次就现形（第 1 次侥幸都是 4 条）。
"""
from __future__ import annotations


def test_b11_preview_no_ghosting(make_game):
    """连画 3 次预览，画布上 preview 标签的对象数必须恒为 4。"""
    game = make_game()
    for _ in range(3):
        game._draw_preview()
        # find_withtag("preview") 返回账本上所有预览图形对象；一个方块 4 格
        assert len(game.canvas.find_withtag("preview")) == 4
