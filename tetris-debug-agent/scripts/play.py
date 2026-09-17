"""启动 buggy 版俄罗斯方块（玩家游玩版本，含 12 个真实编码错误）。"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from game.tetris_buggy import launch


def main() -> None:
    """解析参数并启动游戏。"""
    ap = argparse.ArgumentParser(description="Play the tetris.")
    ap.add_argument("--round", type=int, default=1, help="局号（决定 data/runs/round_N 文件夹名）")
    ap.add_argument("--seed", type=int, default=42, help="随机种子，便于复现")
    args = ap.parse_args()
    launch(round_id=args.round, seed=args.seed)


if __name__ == "__main__":
    main()