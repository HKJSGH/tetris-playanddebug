"""启动 buggy 版俄罗斯方块（玩家游玩版本，含 12 个真实编码错误）。

不填 --round 时自动取已有最大局号 +1，避免覆盖旧局数据。
游戏窗口关闭后自动启动 debug（run_pipeline --campaign，含自动 git 快照），
阻塞至 debug 完成进程才退出——即下一轮游戏只能在修复结束后开始。
debug 因 LLM 不可用（网络故障）中止时，等待 3 分钟自动续跑（最多重试
2 次），重试安全：已完成局已落盘，campaign 从断的那局继续。

debug 结束后的两种走向：
  1. 全部 bug 修复完成（fixes.json status=converged）→ 询问「是否归档本次
     数据并开启新实验？」——确认则 archive_reset 归档并把游戏回退到原始
     12-bug 版（实验迭代入口）；
  2. 未收敛 → 询问「是否立即开始新一轮游戏？」——Y 在本实验内继续游玩
     （新一轮 debug），N 保持现状（玩家可能只是暂时休息，随时重跑本脚本
     继续）。主动终止实验走手动命令：python scripts/archive_reset.py [--tag 实验名]

同进程续玩时每次启动前 importlib.reload 游戏模块：上一轮 debug 写回的
修复补丁必须重载才生效（否则玩家游玩的仍是修复前的旧字节码）。
"""
import argparse
import importlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from game import tetris_buggy  # noqa: E402

RUNS_ROOT = ROOT / "data" / "runs"
FIXES_PATH = ROOT / "data" / "fixes.json"


def next_round() -> int:
    nums = [
        int(m.group(1))
        for p in RUNS_ROOT.glob("round_*")
        if (m := re.fullmatch(r"round_(\d+)", p.name))
    ]
    return max(nums, default=0) + 1


def _converged() -> bool:
    """fixes.json status==converged（全部 bug 修复完成）。"""
    try:
        return json.loads(FIXES_PATH.read_text(encoding="utf-8")).get("status") == "converged"
    except Exception:  # noqa: BLE001 — fixes.json 缺失/损坏视为未收敛
        return False


def _ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _launch_round(rid: int, seed: int) -> None:
    """启动一局游戏；每次游玩前重载游戏模块。

    同进程续玩（debug 后询问「开始新一轮」Y → main() 递归）时，上一轮
    debug 已把修复补丁写回 game/tetris_buggy.py，但 Python 模块缓存里
    仍是修复前的旧代码——不重载的话玩家后续轮次游玩的永远是旧字节码，
    表现为「总结说已修复、实际异常依旧」。
    """
    importlib.reload(tetris_buggy)
    tetris_buggy.launch(round_id=rid, seed=seed)


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
    _launch_round(rid, args.seed)

    if args.no_debug:
        print("已跳过自动 debug（--no-debug），可稍后手动运行 scripts/run_pipeline.py")
        return
    print("游戏已关闭，自动启动 debug（完成后方可开始下一轮游戏）...")
    cmd = [
        sys.executable, str(ROOT / "scripts" / "run_pipeline.py"),
        "--campaign", "--mode", args.mode, "--commit", "--verbose",
    ]
    # debug 中止自动续跑：run_pipeline 退出码 1 = LLMError（网络故障/限流耗尽）
    # 中止，已完成局已落盘有效，campaign 续跑语义会从断的那局重新开始——
    # 等一会儿重试通常就能过。重试 _DEBUG_MAX_ATTEMPTS 次仍失败才交还给人。
    _DEBUG_RETRY_WAIT = 180      # 失败后等待秒数（给网络/限流恢复留时间）
    _DEBUG_MAX_ATTEMPTS = 3      # 含首次共 3 次机会
    attempts = 0
    while True:
        try:
            rc = subprocess.run(cmd, cwd=ROOT).returncode
        except KeyboardInterrupt:
            print("\ndebug 被中断；游玩数据已落盘，可稍后手动运行 scripts/run_pipeline.py 续跑")
            raise SystemExit(130) from None
        if rc == 0 or rc != 1 or attempts >= _DEBUG_MAX_ATTEMPTS - 1:
            break   # 成功 / 非网络类失败（参数错等）/ 重试用尽 → 不再重试
        attempts += 1
        print(f"debug 因 LLM 不可用中止（第 {attempts}/{_DEBUG_MAX_ATTEMPTS - 1} 次重试），"
              f"已完成局已落盘有效；{_DEBUG_RETRY_WAIT // 60} 分钟后自动续跑"
              f"（Ctrl+C 取消）...")
        try:
            time.sleep(_DEBUG_RETRY_WAIT)
        except KeyboardInterrupt:
            print("\n已取消自动续跑；游玩数据已落盘，可稍后手动运行 scripts/run_pipeline.py 续跑")
            raise SystemExit(130) from None
    _post_debug_prompt()
    raise SystemExit(rc)


def _post_debug_prompt() -> None:
    """debug 结束后的分叉询问（见模块 docstring 的两种走向）。"""
    print("-" * 60)
    if _converged():
        # 结束方式 1：全部 bug 修复完成 → 询问归档开新实验
        print("本次实验的全部问题已修复完成！")
        print("开启新实验 = 归档本次对局/评估/日志数据，并把游戏回退到原始 12-bug 版"
              "（agent 迭代平行对比的干净起点）。")
        if _ask("是否归档本次数据并开启新实验？[y/N] "):
            subprocess.run([sys.executable, str(ROOT / "scripts" / "archive_reset.py"), "--yes"],
                           cwd=ROOT)
        else:
            print("已保留本轮数据。可随时手动归档：python scripts/archive_reset.py [--tag 实验名]")
        return
    # 结束方式 2 前的中间态：未收敛 → 询问是否本实验内继续游玩
    if _ask("是否立即开始新一轮游戏？[y/N] "):
        main()
    else:
        print("本轮实验已保留（可随时再次运行 scripts/play.py 继续游玩与 debug）。"
              "主动终止本实验并归档：python scripts/archive_reset.py [--tag 实验名]")


if __name__ == "__main__":
    main()
