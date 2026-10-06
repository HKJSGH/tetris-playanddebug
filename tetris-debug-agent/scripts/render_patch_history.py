r"""渲染补丁尝试历史为可读 markdown（data/runs/round_N/patches.md）。

用法（在沙盒根目录 tetris-debug-agent/ 下执行）：
  python scripts/render_patch_history.py        # 渲染账本中出现过的所有局
  python scripts/render_patch_history.py 11     # 只渲染 round 11

patch_history.jsonl 是 append-only 机读账本；本脚本把它渲染成人工可读的
markdown（失败原因 + 被改函数补丁前后完整源码）。旧条目没有函数快照的
（函数快照功能上线前记录的）退化为 SEARCH/REPLACE 片段展示。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import PATCH_HISTORY_PATH
from agent.nodes.wrapup import render_round_patches


def _known_rounds() -> list[int]:
    rounds: set[int] = set()
    if not PATCH_HISTORY_PATH.exists():
        return []
    for line in PATCH_HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rid = json.loads(line).get("round_id")
        except json.JSONDecodeError:
            continue
        if isinstance(rid, int):
            rounds.add(rid)
    return sorted(rounds)


def main() -> None:
    args = sys.argv[1:]
    rounds = [int(a) for a in args] if args else _known_rounds()
    if not rounds:
        print("patch_history.jsonl 为空或不存在，无内容可渲染")
        return
    for rid in rounds:
        out = render_round_patches(rid)
        print(f"round {rid}: {'→ ' + str(out) if out else '账本中无该局条目，跳过'}")


if __name__ == "__main__":
    main()
