"""每局埋点记录器：在 data/runs/round_N/ 下产出 6 个文件。

文件清单：
  telemetry.jsonl      - 每行一个 JSON 事件（spawn/move/rotate/lock/...，含 ts 时间戳）
  errors.log           - 运行期错误行
  feedback_text.md     - 玩家文字反馈（多条追加保留，带工单号/序号/来源）
  screenshot*.png      - 玩家上传的截图（按工单号命名 screenshot_S0000001_1.png，不覆盖）
  game_state.json      - 终局网格 + 分数 + 当前方块 + 下一个方块
  meta.json            - round_id / seed / 活跃 bug / 工单列表 / 时长 / 版本

反馈工单号：S+7 位全局流水号（data/feedback_seq.txt），一次反馈窗口 = 一个工单，
文字反馈与截图通过工单号关联。本模块不依赖 tkinter：调用方传纯 dict。
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Callable, Optional


# 默认数据落盘根目录：项目根 / data / runs
RUNS_ROOT = Path(__file__).resolve().parent.parent / "data" / "runs"
VERSION = "0.1.0"


class TelemetryRecorder:
    """单局埋点记录器：负责创建 round 目录并流式写入事件。"""

    def __init__(
        self,
        round_id: int,
        seed: int,
        active_bugs: list[str],
        runs_root: Path | str = RUNS_ROOT,
        screenshot_fn: Optional[Callable[[Path], None]] = None,
    ):
        """初始化记录器并创建 round 目录。

        :param round_id: 局号，决定文件夹名 round_N
        :param seed: 随机种子，写入 meta 便于复现
        :param active_bugs: 本局开启的 bug_id 列表
        :param runs_root: 数据根目录
        :param screenshot_fn: 截图捕获函数（玩家上传场景由调用方自行处理，这里可空）
        """
        self.round_id = round_id
        self.seed = seed
        self.active_bugs = list(active_bugs)
        self.runs_root = Path(runs_root)
        self.run_dir = self.runs_root / f"round_{round_id}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._screenshot_fn = screenshot_fn
        self._start_ts = time.time()
        self._end_ts: Optional[float] = None
        self._tel_path = self.run_dir / "telemetry.jsonl"
        self._err_path = self.run_dir / "errors.log"
        self._fb_path = self.run_dir / "feedback_text.md"
        self._feedback_count = 0  # 已收到的反馈条数（用于追加编号）
        self._tickets: list[str] = []  # 本局已分配的反馈工单号
        # 重新运行同一 round_id 时清空旧内容
        self._tel_path.write_text("", encoding="utf-8")
        self._err_path.write_text("", encoding="utf-8")

    # ---- 流式写入 -------------------------------------------------------

    def new_ticket(self) -> str:
        """分配下一个全局反馈工单号（S+7位流水号，跨局持久递增）。

        流水号存于 runs_root 上级目录的 feedback_seq.txt，供工单与截图/文字关联。
        """
        seq_path = self.runs_root.parent / "feedback_seq.txt"
        try:
            cur = int(seq_path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            cur = 0
        cur += 1
        try:
            seq_path.write_text(str(cur), encoding="utf-8")
        except OSError:
            pass
        ticket = f"S{cur:07d}"
        self._tickets.append(ticket)
        return ticket

    def record(self, event: dict) -> None:
        """追加一个事件到 telemetry.jsonl（每行一个 JSON，自动带时间戳）。"""
        line = json.dumps({"ts": round(time.time(), 3), **event}, ensure_ascii=False)
        with self._tel_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def record_error(self, msg: str) -> None:
        """追加一行错误信息到 errors.log。"""
        with self._err_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    # ---- 一次性写入 -----------------------------------------------------

    def save_feedback(self, text: str) -> None:
        """覆盖写反馈文件（仅用于占位，正常反馈用 append_feedback）。"""
        self._fb_path.write_text(text or "(empty)", encoding="utf-8")

    def append_feedback(self, text: str, source: str = "", ticket_id: Optional[str] = None) -> None:
        """追加一条玩家反馈（保留全部历史，带工单号/来源/时间戳，列出该工单附图）。"""
        self._feedback_count += 1
        stamp = time.strftime("%H:%M:%S")
        tid = f" [{ticket_id}]" if ticket_id else ""
        header = f"## 反馈 #{self._feedback_count}{tid} [{source or '未标注'}] {stamp}\n\n"
        body = text.strip() or "(empty)"
        if ticket_id:
            shots = sorted(p.name for p in self.run_dir.glob(f"screenshot_{ticket_id}_*.png"))
            if shots:
                body += "\n\n附图：" + "、".join(shots)
        with self._fb_path.open("a", encoding="utf-8") as f:
            f.write(header + body + "\n")

    def save_uploaded_screenshot(self, src: Path, ticket_id: Optional[str] = None) -> Path:
        """复制玩家上传的截图到 round 目录，按工单号自动编号避免覆盖，返回最终路径。

        有工单号时命名 screenshot_S0000001_1.png；无工单号时沿用 screenshot_N.png。
        """
        src = Path(src)
        if ticket_id:
            n = 1
            while (self.run_dir / f"screenshot_{ticket_id}_{n}.png").exists():
                n += 1
            dest = self.run_dir / f"screenshot_{ticket_id}_{n}.png"
        else:
            dest = self.run_dir / "screenshot.png"
            if dest.exists():
                n = 2
                while (self.run_dir / f"screenshot_{n}.png").exists():
                    n += 1
                dest = self.run_dir / f"screenshot_{n}.png"
        shutil.copy(src, dest)
        return dest

    def save_screenshot(self) -> Path | None:
        """调用截图函数保存 screenshot.png；未提供函数则记错误。"""
        if self._screenshot_fn is None:
            self.record_error("screenshot skipped: no capture function provided")
            return None
        path = self.run_dir / "screenshot.png"
        try:
            self._screenshot_fn(path)
            return path
        except Exception as e:  # noqa: BLE001
            self.record_error(f"screenshot failed: {e!r}")
            return None

    def save_game_state(self, grid: list[list], score: int, lines: int,
                        current_block: Optional[dict], next_kind: Optional[str]) -> None:
        """保存终局状态到 game_state.json。"""
        state = {
            "round_id": self.round_id,
            "score": score,
            "lines_cleared_total": lines,
            "grid": [[cell or "" for cell in row] for row in grid],
            "current_block": current_block,
            "next_kind": next_kind,
        }
        (self.run_dir / "game_state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def finalize(self, score: int, lines: int, outcome: str,
                 grid: list[list], current_block: Optional[dict],
                 next_kind: Optional[str]) -> Path:
        """收尾：截图 + 存状态 + 写 meta.json，返回 round 目录路径。"""
        self._end_ts = time.time()
        # 玩家全程无反馈时写占位，保证文件齐全
        if not self._fb_path.exists():
            self.save_feedback("(no feedback)")
        self.save_screenshot()
        self.save_game_state(grid, score, lines, current_block, next_kind)
        meta = {
            "round_id": self.round_id,
            "seed": self.seed,
            "active_bugs": self.active_bugs,
            "tickets": list(self._tickets),
            "outcome": outcome,
            "score": score,
            "lines_cleared_total": lines,
            "duration_sec": round(self._end_ts - self._start_ts, 3),
            "version": VERSION,
        }
        meta_path = self.run_dir / "meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.run_dir

    # ---- 辅助 -----------------------------------------------------------

    @property
    def duration_sec(self) -> float:
        """已运行时长（秒），未 finalize 时取当前时刻。"""
        end = self._end_ts or time.time()
        return round(end - self._start_ts, 3)
