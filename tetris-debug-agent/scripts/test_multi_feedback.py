"""快速验证：反馈工单号分配、截图按工单命名、文字与截图关联。

模拟一局内 3 个反馈工单（其中 2 个带截图），检查 round 目录最终产物。
"""
import io
import sys
import tempfile
from pathlib import Path

# Windows GBK 控制台下强制 utf-8 输出，避免中文/符号打印报错
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# 保证可从项目根导入 game 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from game.telemetry import TelemetryRecorder  # noqa: E402


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tetris_ticket_test_"))
    rec = TelemetryRecorder(round_id=996, seed=42, active_bugs=[], runs_root=tmp)

    # 流水号全局持久递增，起始值取决于历史运行，断言用相对连续性
    seq_path = tmp.parent / "feedback_seq.txt"
    try:
        n0 = int(seq_path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        n0 = 0

    # 工单 1：截图 1 张 + 文字反馈
    t1 = rec.new_ticket()
    fake1 = tmp / "fake1.png"
    fake1.write_bytes(b"\x89PNG fake1")
    p1 = rec.save_uploaded_screenshot(fake1, ticket_id=t1)

    # 工单 2：仅文字反馈
    t2 = rec.new_ticket()

    # 工单 3：截图 1 张 + 文字反馈
    t3 = rec.new_ticket()
    fake2 = tmp / "fake2.png"
    fake2.write_bytes(b"\x89PNG fake2")
    p2 = rec.save_uploaded_screenshot(fake2, ticket_id=t3)

    rec.append_feedback("O 方块掉得太快了，根本来不及放", source="暂停面板", ticket_id=t1)
    rec.record({"event": "feedback_submitted", "ticket_id": t1, "source": "暂停面板",
                "length": 15, "has_screenshot": True})
    rec.append_feedback("分数怎么变成负数了", source="暂停面板", ticket_id=t2)
    rec.record({"event": "feedback_submitted", "ticket_id": t2, "source": "暂停面板",
                "length": 10, "has_screenshot": False})
    rec.append_feedback("总体感觉左右键是反的", source="游戏结束时", ticket_id=t3)
    rec.record({"event": "feedback_submitted", "ticket_id": t3, "source": "游戏结束时",
                "length": 11, "has_screenshot": True})

    rec.finalize(score=-5, lines=3, outcome="game_over",
                 grid=[["" for _ in range(12)] for _ in range(20)],
                 current_block=None, next_kind="T")

    # ---- 校验 ----
    assert t1 == f"S{n0 + 1:07d}", f"第一个工单号错误: {t1}（期望 S{n0 + 1:07d}）"
    assert t2 == f"S{n0 + 2:07d}", f"第二个工单号错误: {t2}"
    assert t3 == f"S{n0 + 3:07d}", f"第三个工单号错误: {t3}"
    assert p1.name == f"screenshot_{t1}_1.png", f"截图 1 未按工单命名: {p1.name}"
    assert p2.name == f"screenshot_{t3}_1.png", f"截图 2 未按工单命名: {p2.name}"
    assert p1.exists() and p2.exists(), "截图文件未落盘"

    fb = (rec.run_dir / "feedback_text.md").read_text(encoding="utf-8")
    assert f"## 反馈 #1 [{t1}] [暂停面板]" in fb, "反馈 1 工单号缺失"
    assert f"## 反馈 #2 [{t2}] [暂停面板]" in fb, "反馈 2 工单号缺失"
    assert f"## 反馈 #3 [{t3}] [游戏结束时]" in fb, "反馈 3 工单号缺失"
    assert f"附图：screenshot_{t1}_1.png" in fb, "反馈 1 附图关联缺失"
    assert f"附图：screenshot_{t3}_1.png" in fb, "反馈 3 附图关联缺失"
    # 工单 2 无截图，不应有附图行
    seg2 = fb.split(f"## 反馈 #2 [{t2}]")[1].split("## 反馈 #3")[0]
    assert "附图" not in seg2, "无截图工单不应出现附图行"

    import json
    meta = json.loads((rec.run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["tickets"] == [t1, t2, t3], f"meta tickets 错误: {meta['tickets']}"

    lines = (rec.run_dir / "telemetry.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert all('"ts"' in ln for ln in lines), "事件缺少 ts 时间戳"
    assert any('"ticket_id"' in ln for ln in lines), "事件缺少 ticket_id"

    print("全部校验通过 ✓")
    print(f"round 目录: {rec.run_dir}")
    print("--- feedback_text.md ---")
    print(fb)
    for f in sorted(rec.run_dir.iterdir()):
        print(f"  {f.name}  ({f.stat().st_size} bytes)")


if __name__ == "__main__":
    main()