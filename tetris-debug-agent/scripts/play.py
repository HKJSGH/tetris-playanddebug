"""启动 buggy 版俄罗斯方块（玩家游玩版本，含 12 个真实编码错误）。

不填 --round 时自动取已有最大局号 +1，避免覆盖旧局数据。
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from game.tetris_buggy import launch

RUNS_ROOT = Path(__file__).resolve().parent.parent / "data" / "runs"


def next_round() -> int:
    nums = [
        int(m.group(1))
        for p in RUNS_ROOT.glob("round_*")
        if (m := re.fullmatch(r"round_(\d+)", p.name))
    ]
    return max(nums, default=0) + 1


def main() -> None:
    """解析参数并启动游戏。"""
    ap = argparse.ArgumentParser(description="Play the tetris.")
    ap.add_argument("--round", type=int, default=None,
                    help="局号（决定 data/runs/round_N 文件夹名）；缺省=已有最大局号+1")
    ap.add_argument("--seed", type=int, default=42, help="随机种子，便于复现")
    args = ap.parse_args()
    rid = args.round if args.round is not None else next_round()
    print(f"对局数据将写入 data/runs/round_{rid}/（seed={args.seed}）")
    launch(round_id=rid, seed=args.seed)


if __name__ == "__main__":
    main()
