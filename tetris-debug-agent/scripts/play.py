"""启动 buggy 版俄罗斯方块（玩家游玩版本，含 12 个真实编码错误）。

不填 --round 时自动取已有最大局号 +1，避免覆盖旧局数据。
游戏窗口关闭后自动启动 debug（run_pipeline --campaign，含自动 git 快照），
阻塞至 debug 完成进程才退出——即下一轮游戏只能在修复结束后开始。
debug 结束后控制台询问「是否归档本次数据并开启新实验」：确认则
archive_reset.py 归档本轮全部数据并把游戏回退到原始 12-bug 版（实验迭代入口）；
拒绝则保留现状，可随时手动运行 scripts/archive_reset.py。
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from game.tetris_buggy import launch  # noqa: E402

RUNS_ROOT = ROOT / "data" / "runs"


def next_round() -> int:
    nums = [
        int(m.group(1))
        for p in RUNS_ROOT.glob("round_*")
        if (m := re.fullmatch(r"round_(\d+)", p.name))
    ]
    return max(nums, default=0) + 1


def main() -> None:
    """解析参数并启动游戏，结束后自动 debug。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Play the tetris, then auto-debug.")
    ap.add_argument("--round", type=int, default=None,
                    help="局号（决定 data/runs/round_N 文件夹名）；缺省=已有最大局号+1")
    ap.add_argument("--seed", type=int, default=42, help="随机种子，便于复现")
    ap.add_argument("--mode", choices=["mock", "llm"], default="llm",
                    help="游戏结束后自动 debug 的模式（默认 llm）")
    ap.add_argument("--no-debug", action="store_true",
                    help="关闭游戏后不自动启动 debug（之后可手动跑 run_pipeline）")
    args = ap.parse_args()
    rid = args.round if args.round is not None else next_round()
    print(f"对局数据将写入 data/runs/round_{rid}/（seed={args.seed}）")
    launch(round_id=rid, seed=args.seed)

    if args.no_debug:
        print("已跳过自动 debug（--no-debug），可稍后手动运行 scripts/run_pipeline.py")
        return
    print("游戏已关闭，自动启动 debug（完成后方可开始下一轮游戏）...")
    cmd = [
        sys.executable, str(ROOT / "scripts" / "run_pipeline.py"),
        "--campaign", "--mode", args.mode, "--commit", "--verbose",
    ]
    try:
        rc = subprocess.run(cmd, cwd=ROOT).returncode
    except KeyboardInterrupt:
        print("\ndebug 被中断；游玩数据已落盘，可稍后手动运行 scripts/run_pipeline.py 续跑")
        raise SystemExit(130) from None
    _ask_new_experiment()
    raise SystemExit(rc)


def _ask_new_experiment() -> None:
    """debug 结束后询问是否归档并开启新实验（实验迭代循环的入口）。

    确认 → archive_reset --yes：归档本轮数据（runs/eval/logs/fixes/修复态游戏）
    并把游戏回退到原始 12-bug 版、清零工单号，下一局从 round_1 重新开始；
    拒绝 → 保持现状（可稍后手动运行 scripts/archive_reset.py）。
    """
    print("-" * 60)
    print("本轮 debug 已结束。开启新实验 = 归档本次对局/评估/日志数据，"
          "并把游戏回退到原始 12-bug 版（agent 迭代平行对比的干净起点）。")
    try:
        resp = input("是否归档本次数据并开启新实验？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        resp = ""
    if resp in ("y", "yes"):
        subprocess.run([sys.executable, str(ROOT / "scripts" / "archive_reset.py"), "--yes"],
                       cwd=ROOT)
    else:
        print("已保留本轮数据。可随时手动归档：python scripts/archive_reset.py [--tag 实验名]")


if __name__ == "__main__":
    main()
