"""俄罗斯方块 游戏

功能：12×20 网格 + 七种方块 + 行为埋点落盘 + Esc 暂停面板 + 玩家反馈窗口 + 截图上传。
每局结束写入 data/runs/round_N/（telemetry.jsonl / errors.log / feedback_text.md /
screenshot*.png / game_state.json / meta.json）。

反馈工单：一次反馈窗口分配一个全局工单号（S+7 位），文字与截图按工单关联。

通过 scripts/play.py 启动；
"""
from __future__ import annotations

import random
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

from game.telemetry import TelemetryRecorder, RUNS_ROOT

# ---- 布局与形状常量 ------------------------------------------------------
cell_size = 30       # 每格像素
C = 12               # 列数
R = 20               # 行数
height = R * cell_size
side_width = 160     # 右侧信息栏宽度
width = C * cell_size + side_width
FPS = 200            # 主循环间隔（毫秒）

# 7 种方块的相对坐标定义（原点为方块的 cr 位置）
SHAPES = {
    "O": [(-1, -1), (0, -1), (-1, 0), (0, 0)],
    "S": [(-1, 0), (0, 0), (0, -1), (1, -1)],
    "T": [(-1, 0), (0, 0), (0, -1), (1, 0)],
    "I": [(0, 1), (0, 0), (0, -1), (0, -2)],
    "L": [(-1, 0), (0, 0), (-1, -1), (-1, -2)],
    "J": [(-1, 0), (0, 0), (0, -1), (0, -2)],
    "Z": [(-1, -1), (0, -1), (0, 0), (1, 0)],
}

# 各方块颜色（经典配色：I=青 Z=红 O=黄 T=紫 S=绿 J=蓝 L=橙）
COLORS = {
    "O": "yellow", "S": "green", "T": "purple", "I": "red",
    "L": "orange", "J": "blue", "Z": "red",
}

# 预览区独立渲染使用的色表，与 COLORS 保持一致
PREVIEW_COLORS = {
    "O": "yellow", "S": "green", "T": "purple", "I": "Cyan",
    "L": "orange", "J": "blue", "Z": "red",
}


class TetrisGame:
    """俄罗斯方块 tkinter 游戏：含埋点、暂停面板、反馈窗口。"""

    def __init__(
        self,
        round_id: int = 1,
        seed: int = 42,
        runs_root: Path | str = RUNS_ROOT,
        initial_pos: str | None = None,
    ):
        """初始化游戏状态、UI 与埋点记录器。

        :param round_id: 局号，决定数据文件夹名 round_N
        :param seed: 随机种子，便于复现
        :param runs_root: 数据落盘根目录
        :param initial_pos: 窗口初始位置（如 "+100+80"），None 用系统默认
        """
        self.seed = seed
        self.rng = random.Random(seed)
        self.round_id = round_id
        self._initial_pos = initial_pos
        # 埋点记录器：截图由玩家在反馈窗口上传，这里不传自动截图函数
        self.rec = TelemetryRecorder(
            round_id=round_id,
            seed=seed,
            active_bugs=[],
            runs_root=runs_root,
            screenshot_fn=None,
        )

        self.score = 0
        self.lines_cleared_total = 0
        # 已落定方块网格：block_list[row][col] = 方块类型字符串或空串
        self.block_list: list[list[str]] = [["" for _ in range(C)] for _ in range(R)]
        self.current_block: Optional[dict] = None
        self.next_kind: str = self._pick_kind()
        self.paused = False
        self.game_over = False
        self._outcome: str = ""                # 暂存游戏结束原因
        self._cancel_id: Optional[str] = None  # after() 句柄，便于取消
        self._finalized: bool = False          # 是否已收尾（防重复落盘）
        self._play_again: bool = False         # 结算时玩家是否选择再来一局
        self._final_pos: str | None = None     # 收尾时捕获的窗口位置，供下一局恢复
        self._pause_panel: Optional[tk.Toplevel] = None    # 暂停面板
        self._feedback_win: Optional[tk.Toplevel] = None   # 反馈窗口（防重复打开）

        self._setup_ui()
        self._bind_keys()
        self._schedule(FPS)
        self.rec.record({"event": "round_start"})

    # ---- UI 初始化 ------------------------------------------------------

    def _setup_ui(self) -> None:
        """创建主窗口、画布、分数区、预览区、操作提示。"""
        self.win = tk.Tk()
        self.win.title(f"Tetris — round {self.round_id}")
        if self._initial_pos:
            self.win.geometry(self._initial_pos)
        self.canvas = tk.Canvas(self.win, width=width, height=height, bg="white")
        self.canvas.pack()
        board_w = C * cell_size
        # 主板与侧栏的分隔线
        self.canvas.create_line(board_w, 0, board_w, height, fill="#999999", width=2)
        # 分数标签与数值
        self.canvas.create_text(board_w + side_width // 2, 20, text="分数", font=("Arial", 12, "bold"))
        self.score_text_id = self.canvas.create_text(
            board_w + side_width // 2, 50, text="0", font=("Arial", 20, "bold"), fill="red", tag="score"
        )
        # 预览区标题
        self.canvas.create_text(board_w + side_width // 2, 110, text="下一个", font=("Arial", 12, "bold"))
        self._preview_tag = "preview"
        # 操作提示
        self.canvas.create_text(
            board_w + side_width // 2, height - 90,
            text="← → 移动 / ↑ 旋转 / ↓ 硬降\nEsc 暂停",
            font=("Arial", 9), fill="#666", tag="hint"
        )
        # 点窗口关闭按钮也走正常收尾，防止数据丢失
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

    def _bind_keys(self) -> None:
        """绑定键盘事件到对应 handler。"""
        self.canvas.focus_set()
        self.canvas.bind("<KeyPress-Left>", self._on_left)
        self.canvas.bind("<KeyPress-Right>", self._on_right)
        self.canvas.bind("<KeyPress-Up>", self._on_rotate)
        self.canvas.bind("<KeyPress-Down>", self._on_land)
        self.canvas.bind("<KeyPress-Escape>", self._on_pause)

    # ---- 辅助方法 -------------------------------------------------------

    def _pick_kind(self) -> str:
        """随机选一个方块类型。"""
        return self.rng.choice(list(SHAPES.keys()))

    def _color_for(self, kind: str) -> str:
        """取方块颜色。"""
        return COLORS[kind]

    def _draw_cell(self, c: int, r: int, color: str, tag: str = "") -> None:
        """在画布上绘制单个格子。

        :param tag: "falling"=下落中方块；"row"=已落定方块（带 row-r tag 便于清除）；其他=无 tag
        """
        x0, y0 = c * cell_size, r * cell_size
        x1, y1 = x0 + cell_size, y0 + cell_size
        if tag == "falling":
            self.canvas.create_rectangle(x0, y0, x1, y1, fill=color, outline="white", width=2, tag=tag)
        elif tag == "row":
            self.canvas.create_rectangle(x0, y0, x1, y1, fill=color, outline="white", width=2, tag=f"row-{r}")
        else:
            self.canvas.create_rectangle(x0, y0, x1, y1, fill=color, outline="white", width=2)

    def _draw_board(self) -> None:
        """重绘整个已落定网格（先删后绘）。"""
        for ri in range(R):
            self.canvas.delete(f"row-{ri}")
        for ri in range(R):
            for ci in range(C):
                t = self.block_list[ri][ci]
                if t:
                    self._draw_cell(ci, ri, self._color_for(t), tag="row")

    def _draw_cells(self, c: int, r: int, cell_list, color: str) -> None:
        """按相对坐标绘制一个方块的所有格子（仅画落在画布内的）。"""
        for cell in cell_list:
            cc, cr = cell
            ci, ri = cc + c, cr + r
            if 0 <= ci < C and 0 <= ri < R:
                self._draw_cell(ci, ri, color, tag="falling")

    def _draw_preview(self) -> None:
        """在侧栏绘制预览方块（半尺寸）。"""
        cell_list = SHAPES[self.next_kind]
        board_w = C * cell_size
        pc, pr = board_w + side_width // 2 - cell_size, 130
        for cell in cell_list:
            cc, cr = cell
            x0 = pc + cc * cell_size // 2 + cell_size
            y0 = pr + cr * cell_size // 2 + cell_size
            self.canvas.create_rectangle(
                x0, y0, x0 + cell_size // 2, y0 + cell_size // 2,
                fill=PREVIEW_COLORS[self.next_kind], outline="white", tag=self._preview_tag
            )
        self.rec.record({"event": "preview", "kind": self.next_kind})

    def _update_score_display(self) -> None:
        """刷新分数显示与窗口标题。"""
        self.canvas.itemconfig(self.score_text_id, text=str(self.score))
        self.win.title(f"Tetris — round {self.round_id}  score={self.score}")

    # ---- 方块操作 -------------------------------------------------------

    def _check_move(self, block: dict, direction=(0, 0)) -> bool:
        """判断方块能否沿指定方向移动（边界 + 已占格检测）。"""
        cc, cr = block["cr"]
        cell_list = block["cell_list"]
        for cell in cell_list:
            cc2, cr2 = cell
            c = cc2 + cc + direction[0]
            r = cr2 + cr + direction[1]
            if c < 0 or c >= C or r >= R:
                return False
            if r >= 0 and self.block_list[r][c]:
                return False
        return True

    def _generate_new_block(self) -> dict:
        """生成下一个方块：取 next_kind，滚动预览，记录 spawn 事件。"""
        kind = self.next_kind
        self.next_kind = self._pick_kind()
        self._draw_preview()
        block = {"kind": kind, "cell_list": SHAPES[kind], "cr": [C // 2, 0]}
        self.rec.record({"event": "spawn", "piece": kind, "cr": block["cr"]})
        return block

    def _save_block_to_list(self, block: dict) -> None:
        """将方块写入网格数据并重绘已落定格子。"""
        self.canvas.delete("falling")
        kind = block["kind"]
        cc, cr = block["cr"]
        for cell in block["cell_list"]:
            cc2, cr2 = cell
            c, r = cc2 + cc, cr2 + cr
            if 0 <= r < R and 0 <= c < C:
                self.block_list[r][c] = kind
        self.rec.record({"event": "lock", "piece": kind, "cr": block["cr"]})
        # 落定后立即重绘，保证方块留在画面上
        self._draw_board()

    def _check_and_clear(self) -> None:
        """检测并消除满行，更新分数。"""
        cleared = 0
        for ri in range(len(self.block_list)):
            if all(self.block_list[ri]):
                cleared += 1
                if ri > 0:
                    for cur_ri in range(ri, 0, -1):
                        self.block_list[cur_ri] = self.block_list[cur_ri - 1][:]
                    self.block_list[0] = ["" for _ in range(C)]
                else:
                    self.block_list[ri] = ["" for _ in range(C)]
                break
        if cleared:
            self.lines_cleared_total += cleared
            self.rec.record({"event": "line_clear", "count": cleared, "score": self.score})
            self._draw_board()
            self._update_score_display()

    # ---- 输入处理 -------------------------------------------------------

    def _on_left(self, event) -> None:
        """左移一格。"""
        if self.game_over or self.paused:
            return
        self._try_move([-1, 0], action="left")

    def _on_right(self, event) -> None:
        """右移一格。"""
        if self.game_over or self.paused:
            return
        self._try_move([-1, 0], action="right")

    def _try_move(self, direction, action: str) -> None:
        """尝试移动并记录（可移动才执行）。"""
        if self.current_block and self._check_move(self.current_block, direction):
            self._draw_block_move(self.current_block, direction)
            self.rec.record({"event": "move", "action": action, "cr": self.current_block["cr"]})

    def _draw_block_move(self, block: dict, direction=(0, 0)) -> None:
        """清除旧位置、移动方块数据、在新位置重绘。"""
        self.canvas.delete("falling")
        dc, dr = direction
        block["cr"] = [block["cr"][0] + dc, block["cr"][1] + dr]
        c, r = block["cr"]
        self._draw_cells(c, r, block["cell_list"], self._color_for(block["kind"]))

    def _on_rotate(self, event) -> None:
        """旋转：检查碰撞，可旋转才生效。"""
        if self.game_over or self.paused or self.current_block is None:
            return
        block = self.current_block
        # 旋转：(c, r) -> (r, -c)
        rotate_list = [[cell[1], -cell[0]] for cell in block["cell_list"]]
        rotated = {"kind": block["kind"], "cell_list": rotate_list, "cr": block["cr"]}
        if self._check_move(rotated):
            self.canvas.delete("falling")
            self._draw_cells(block["cr"][0], block["cr"][1], rotate_list, self._color_for(block["kind"]))
            self.current_block = rotated
            self.rec.record({"event": "rotate", "cr": block["cr"]})

    def _on_land(self, event) -> None:
        """硬降：计算到落底距离，一次性下移。"""
        if self.game_over or self.paused or self.current_block is None:
            return
        block = self.current_block
        cc, cr = block["cr"]
        min_height = R
        for cell in block["cell_list"]:
            cc2, cr2 = cell
            c, r = cc2 + cc, cr2 + cr
            if r >= 0 and self.block_list[r][c]:
                return
            h = 0
            for ri in range(r + 1, r + 1):
                if self.block_list[ri][c]:
                    break
                h += 1
            if h < min_height:
                min_height = h
        if self._check_move(block, [0, min_height]):
            self._draw_block_move(block, [0, min_height])
            self.rec.record({"event": "hard_drop", "distance": min_height, "cr": block["cr"]})

    # ---- 暂停与面板 -----------------------------------------------------

    def _on_pause(self, event) -> None:
        """Esc 切换暂停/恢复；暂停时弹出操作面板。"""
        if self.game_over:
            return
        if self.paused:
            self._resume()
        else:
            self.paused = True
            if self._cancel_id:
                try:
                    self.win.after_cancel(self._cancel_id)
                except Exception:
                    pass
                self._cancel_id = None
            self.rec.record({"event": "pause", "success": True})
            self._open_pause_panel()

    def _open_pause_panel(self) -> None:
        """弹出暂停面板：进入反馈 / 重新开始，按 Esc 继续游戏。"""
        panel = tk.Toplevel(self.win)
        panel.title("已暂停")
        panel.transient(self.win)
        # 初始位置：屏幕正中间
        w, h = 260, 160
        x = (panel.winfo_screenwidth() - w) // 2
        y = (panel.winfo_screenheight() - h) // 2
        panel.geometry(f"{w}x{h}+{x}+{y}")
        self._pause_panel = panel
        tk.Label(panel, text="游戏已暂停", font=("Arial", 12, "bold")).pack(pady=10)
        tk.Button(panel, text="进入反馈", width=14,
                  command=lambda: self._open_feedback_window(blocking=False)).pack(pady=3)
        tk.Button(panel, text="重新开始", width=14, command=self._restart_round).pack(pady=3)
        tk.Label(panel, text="按 Esc 继续游戏", font=("Arial", 9), fg="#666").pack(pady=4)
        panel.bind("<Escape>", self._resume)

    def _resume(self, event=None) -> None:
        """从暂停恢复：关闭面板、重启主循环。"""
        if self.paused:
            return
        if self._pause_panel is not None:
            try:
                self._pause_panel.destroy()
            except Exception:
                pass
            self._pause_panel = None
        self.paused = False
        self.rec.record({"event": "resume", "success": True})
        self._schedule(FPS)

    def _restart_round(self) -> None:
        """手动结束本轮并准备开下一局。"""
        if self._finalized:
            return
        self.rec.record({"event": "manual_restart", "score": self.score})
        self._do_finalize("manual_restart")

    def _on_close(self, event=None) -> None:
        """点窗口关闭按钮：未收尾则按 window_closed 收尾，防数据丢失。"""
        if self._finalized:
            try:
                self.win.destroy()
            except Exception:
                pass
            return
        self.rec.record({"event": "window_closed", "score": self.score})
        self._do_finalize("window_closed")

    # ---- 反馈窗口 -------------------------------------------------------

    def _open_feedback_window(self, blocking: bool = True) -> None:
        """打开反馈窗口：一次窗口 = 一个工单号，文字与截图按工单关联。

        :param blocking: True=游戏结束时（模态，提交/跳过后 finalize）；False=暂停面板入口（提交后仅保存）
        """
        # 防重复打开：已开着则聚焦
        if self._feedback_win is not None and self._feedback_win.winfo_exists():
            self._feedback_win.lift()
            self._feedback_win.focus_force()
            return
        ticket = self.rec.new_ticket()
        win = tk.Toplevel(self.win)
        self._feedback_win = win
        win.title(f"玩家反馈 — 工单 {ticket}")
        win.transient(self.win)
        # 初始尺寸放大一倍，位置：屏幕正中间，保证截图入口可见
        w, h = 880, 720
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 2
        win.geometry(f"{w}x{h}+{x}+{y}")

        tk.Label(win, text=f"工单 {ticket}：描述你遇到的问题（看到的、感觉不对的）：").pack(anchor="w", padx=8, pady=4)
        txt = tk.Text(win, height=12, width=52)
        txt.pack(padx=8, fill="both", expand=True)

        # 截图上传区（本工单内可上传多张，自动编号）
        uploaded: list[str] = []
        label = tk.Label(win, text="未上传截图", fg="#888")
        label.pack(pady=2)

        def upload_screenshot() -> None:
            """弹出文件选择框，让玩家选本地图片复制到 round 目录。"""
            path = filedialog.askopenfilename(
                title="选择游戏截图",
                filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp"), ("所有文件", "*.*")],
            )
            if not path:
                return
            try:
                dest = self.rec.save_uploaded_screenshot(Path(path), ticket_id=ticket)
                uploaded.append(dest.name)
                label.config(text=f"已上传：{dest.name}", fg="green")
                self.rec.record({"event": "screenshot_uploaded", "ticket_id": ticket, "path": str(dest)})
            except Exception as e:  # noqa: BLE001
                label.config(text=f"上传失败：{e}", fg="red")
                self.rec.record_error(f"screenshot upload failed: {e!r}")

        tk.Button(win, text="上传截图（可选）", command=upload_screenshot).pack(pady=2)

        def submit() -> None:
            """提交反馈：追加保存（保留历史），关闭窗口。blocking 时触发 finalize。"""
            text = txt.get("1.0", "end").strip()
            source = "游戏结束时" if blocking else "暂停面板"
            self.rec.append_feedback(text, source=source, ticket_id=ticket)
            self.rec.record({
                "event": "feedback_submitted",
                "ticket_id": ticket,
                "source": source,
                "length": len(text),
                "has_screenshot": bool(uploaded),
            })
            win.destroy()
            if blocking:
                self._do_finalize(self._outcome)

        def skip() -> None:
            """跳过反馈：工单作废，关闭窗口。blocking 时继续 finalize。"""
            self.rec.record({"event": "feedback_skipped", "ticket_id": ticket})
            win.destroy()
            if blocking:
                self._do_finalize(self._outcome)

        btn_frame = tk.Frame(win)
        btn_frame.pack(pady=6)
        tk.Button(btn_frame, text="提交并结束" if blocking else "提交", command=submit).pack(side="left", padx=8)
        tk.Button(btn_frame, text="跳过" if blocking else "关闭", command=skip).pack(side="left", padx=8)

    # ---- 游戏主循环 -----------------------------------------------------

    def _schedule(self, ms: int) -> None:
        """安排下一次 _game_loop 调用（先取消旧的）。"""
        if self._cancel_id:
            try:
                self.win.after_cancel(self._cancel_id)
            except Exception:
                pass
        self._cancel_id = self.win.after(ms, self._game_loop)

    def _game_loop(self) -> None:
        """主循环：自动下落 / 落底锁定 / 消行 / 生成新块 / 顶部碰撞即结束。"""
        if self.game_over or self.paused:
            return
        self.win.update()
        if self.current_block is None:
            # 生成新块
            new_block = self._generate_new_block()
            self._draw_block_move(new_block)
            self.current_block = new_block
        else:
            # 尝试下落一格
            if self._check_move(self.current_block, [0, 1]):
                self._draw_block_move(self.current_block, [0, 1])
                self.rec.record({"event": "fall", "cr": self.current_block["cr"]})
            else:
                # 落底：写入网格、消除满行
                self._save_block_to_list(self.current_block)
                self.current_block = None
                self._check_and_clear()

        if self.current_block is not None and self.current_block["kind"] == "O":
            self._schedule(FPS // 5)
        else:
            self._schedule(FPS)

    # ---- 结束流程 -------------------------------------------------------

    def _finalize(self, outcome: str) -> None:
        """进入结束流程：弹模态反馈窗口，玩家提交/跳过后回调 _do_finalize。"""
        if self.game_over:
            return
        self.game_over = True
        self._outcome = outcome
        self._open_feedback_window(blocking=True)

    def _do_finalize(self, outcome: str) -> None:
        """收尾：停机、落盘数据、弹结算询问、销毁窗口。"""
        if self._finalized:
            return
        self._finalized = True
        self._final_pos = f"+{self.win.winfo_x()}+{self.win.winfo_y()}"
        self.game_over = True
        self._outcome = outcome
        # 停掉主循环与可能开着面板
        if self._cancel_id:
            try:
                self.win.after_cancel(self._cancel_id)
            except Exception:
                pass
            self._cancel_id = None
        if self._pause_panel is not None:
            try:
                self._pause_panel.destroy()
            except Exception:
                pass
            self._pause_panel = None
        self.canvas.delete("falling")
        self.rec.record({"event": "game_over", "outcome": outcome, "score": self.score})
        self.rec.finalize(
            score=self.score,
            lines=self.lines_cleared_total,
            outcome=outcome,
            grid=self.block_list,
            current_block=self.current_block,
            next_kind=self.next_kind,
        )
        try:
            self._play_again = messagebox.askyesno(
                "本局结束",
                f"Your score is {self.score}\n数据已保存到 {self.rec.run_dir}\n\n再来一局？",
            )
        except Exception:
            self._play_again = False
        try:
            self.win.destroy()
        except Exception:
            pass

    def run(self) -> None:
        """启动 tkinter 主循环。"""
        self.win.mainloop()


def launch(
    round_id: int = 1,
    seed: int = 42,
    runs_root: Path | str = RUNS_ROOT,
) -> None:
    """启动游戏，结算后玩家选择是否再来一局（局号自动递增）。"""
    n = round_id
    while True:
        pos: str | None = None
        game = TetrisGame(round_id=n, seed=seed, runs_root=runs_root, initial_pos=pos)
        game.run()
        pos = game._final_pos
        if not game._play_again:
            break
        n += 1
