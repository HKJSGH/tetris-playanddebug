"""启动 buggy 版俄罗斯方块（玩家游玩版本，含 12 个真实编码错误）。

不填 --round 时自动取已有最大局号 +1，避免覆盖旧局数据。
游戏窗口关闭后自动启动 debug（run_pipeline --campaign，含自动 git 快照），
阻塞至 debug 完成进程才退出——即下一轮游戏只能在修复结束后开始。
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
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
