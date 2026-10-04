"""B02：旋转必须对「旋转后的新姿态」做碰撞检查，不得穿入已占格。

【玩家可见现象】
方块贴近墙或死块时按旋转，会直接转进障碍物身体里（穿模）。

【注入的缺陷】
_on_rotate 里边界检查写错了对象：`if self._check_move(block):` 检查的是
**旋转前的旧方块**（它当然不撞），而不是旋转后的新姿态 `rotated`。
修复 = 把检查对象换成 rotated。

【测试思路】
摆一个「旋转必撞」的局面：死块砌在行 11，方块悬在其上方，旋转一次
恰好压进死块。此时：
  - buggy（查旧方块）：旧位置不撞 → 放行旋转 → 新姿态穿模 → 红
  - 修复（查新姿态）：新位置撞 → 拒绝旋转，方块原地不动 → 绿

【几何细节（为什么用 O 块也能测）】
旋转公式是 (c, r) -> (r, -c)，绕**坐标系原点**转、不是绕方块中心。
所以 O 块虽然形状不变，绝对位置却会整体下移一行：
  O 块四格（相对）：(-1,-1) (0,-1) (-1,0) (0,0)
  摆在 [5,10]（列5 行10）后占：列 4~5，行 9~10
  旋转后占：            列 4~5，行 10~11  ← 下移一行
而死块恰好在行 11 列 4~5——正是旋转后 O 会落进去的位置。
形状不变反而排除了形状变化的干扰：失败只可能来自「该拒绝的旋转没拒绝」。
"""
from __future__ import annotations

C, R = 12, 20   # 棋盘：12 列 × 20 行（与游戏常量一致，行号 0~19）


def _cells(game) -> set:
    """当前方块占据的绝对格子坐标集合 {(列, 行), ...}。"""
    cc, cr = game.current_block["cr"]   # cr 存的是 [列, 行]
    return {(c + cc, r + cr) for c, r in game.current_block["cell_list"]}


def test_b02_rotation_rejected_when_colliding(make_game, force_block, set_grid):
    """旋转会撞进死块时，必须被拒绝（方块保持原地）。"""
    game = make_game()
    # 砌死墙：第 11 行、列 4 和列 5 各一块（set_grid 参数是 (行, 列, 类型)）
    set_grid(game, [(11, 4, "J"), (11, 5, "L")])
    # O 块摆在 [5,10]：占列 4~5、行 9~10，正好悬在死块正上方
    force_block(game, "O", [5, 10])
    game._on_rotate(None)               # 按旋转（直接调用函数，不用真按键盘）
    occupied = {(c, r) for r in range(R) for c in range(C) if game.block_list[r][c]}
    # 修复后：旋转被拒绝，方块还占着行 9~10，与行 11 的死块无交集
    assert not (_cells(game) & occupied)


def test_b02_rotation_applies_when_free(make_game, force_block):
    """对照：空旷处旋转必须真的生效（防「把旋转功能整个删掉」式假修复）。"""
    game = make_game()
    force_block(game, "O", [5, 10])
    before = [list(c) for c in game.current_block["cell_list"]]
    game._on_rotate(None)
    assert [list(c) for c in game.current_block["cell_list"]] != before
