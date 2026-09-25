r"""archive_reset — 实验归档与游戏回退（开发者工具，pipeline 不调用）。

一条命令完成一轮实验的收尾，支撑「调整 agent → 新实验 → 平行对比」的迭代循环：
  1. 种子：首次运行生成 game/tetris_buggy.py.orig——原始 12-bug 版。来源：
     出题方 tetris-game/game/tetris_buggy_v0.py 剥掉 12 条 `# NOTE:` 注释
     （注释只经 catalog.yaml 通道进 agent，绝不随源码进沙盒）。三重校验：
     ast.parse 语法 / grep NOTE=0 / 与 data/backup/tetris_buggy.attempt1.py
     逐字节一致（attempt1 是首次补丁前的备份，即权威原始态）。
     .orig 不可被 import，pytest 与 srcmap 均不触及，agent 完全无感知。
  2. 归档（移动，原位置清空）：archives/<时间戳[_tag]>/ 下收
       game/tetris_buggy.py   —— 本轮修复态快照
       data/fixes.json        —— 跨局修复记忆
       data/feedback_seq.txt  —— 工单流水号（如存在）
       data/runs/ data/backup/ eval/ logs/
     并写 archive_manifest.json（时间/tag/修复清单/关键文件 sha256）。
  3. 重置：game/tetris_buggy.py ← .orig（回到 12-bug 原始态）；
     写空 fixes.json；删 feedback_seq.txt（工单号回 S0000001）；
     runs/eval/logs/backup 留空目录。play.py 下一局自动从 round_1 开始。

评估报告（12-bug 修复清单）不入此脚本：由出题方侧
  python tetris-game/scripts/score_eval.py --archive archives/<目录>
join truth_map 生成，写入沙盒 eval_reports/（该目录入库，archives/ gitignore）。

用法：
  python scripts/archive_reset.py [--tag 实验名] [--dry-run] [--yes]
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # tetris-debug-agent/ 沙盒
GAME_ROOT = ROOT.parent / "tetris-game"                # 出题方侧（仅种子步骤读 v0）
V0_PATH = GAME_ROOT / "game" / "tetris_buggy_v0.py"
ORIG_PATH = ROOT / "game" / "tetris_buggy.py.orig"
TARGET_PATH = ROOT / "game" / "tetris_buggy.py"
ARCHIVES_ROOT = ROOT / "archives"
FIXES_PATH = ROOT / "data" / "fixes.json"
FEEDBACK_SEQ = ROOT / "data" / "feedback_seq.txt"

# 归档范围：相对沙盒根的目录（整体移走后重建空目录）与单文件
ARCHIVE_DIRS = ["data/runs", "data/backup", "eval", "logs"]
ARCHIVE_FILES = ["game/tetris_buggy.py", "data/fixes.json", "data/feedback_seq.txt"]

EMPTY_FIXES = {
    "version": 1,
    "fixed_phenomena": [],
    "rejected": [],
    "remaining": [],
    "status": "open",
}


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def seed_orig(dry_run: bool = False) -> None:
    """确保 game/tetris_buggy.py.orig 存在且可信（原始 12-bug 版，无 NOTE）。"""
    if ORIG_PATH.exists():
        text = ORIG_PATH.read_text(encoding="utf-8")
        if "# NOTE:" in text:
            raise SystemExit(f".orig 含 NOTE 注释（泄漏风险），请人工检查：{ORIG_PATH}")
        ast.parse(text)  # 语法兜底
        return
    if not V0_PATH.exists():
        raise SystemExit(f"找不到出题方基线 {V0_PATH}，无法生成 .orig 种子")
    v0_text = V0_PATH.read_text(encoding="utf-8")
    text = "\n".join(l for l in v0_text.splitlines() if "# NOTE:" not in l) + "\n"
    # 校验 1：语法完整
    ast.parse(text)
    # 校验 2：剥注释后必须无 NOTE 残留（防将来注释格式变化）
    if "# NOTE:" in text:
        raise SystemExit("剥注释后仍有 NOTE 残留，中止")
    # 校验 3：与首次补丁前的备份逐字节一致（权威原始态交叉验证）
    attempt1 = ROOT / "data" / "backup" / "tetris_buggy.attempt1.py"
    if attempt1.exists() and attempt1.read_text(encoding="utf-8").strip() != text.strip():
        raise SystemExit(
            f"v0 剥 NOTE 与 {attempt1} 不一致：出题方基线与原始态失同步，拒绝生成 .orig"
        )
    if dry_run:
        print(f"[dry-run] 将生成 {ORIG_PATH}（{len(text.splitlines())} 行，校验通过）")
        return
    ORIG_PATH.write_text(text, encoding="utf-8")
    print(f"已生成原始版种子: {ORIG_PATH}（12-bug 原始态，无 NOTE）")


def _collect_state() -> dict:
    """归档前读取本轮状态摘要（fail-safe：文件缺失视为无数据）。"""
    state: dict = {"rounds": 0, "status": "-", "fixed": [], "rejected_count": 0}
    fx = FIXES_PATH
    if fx.exists():
        try:
            data = json.loads(fx.read_text(encoding="utf-8"))
            state["status"] = data.get("status", "open")
            state["fixed"] = [
                {"phenomenon_id": f.get("phenomenon_id", ""),
                 "hypothesis": str(f.get("hypothesis", ""))[:120],
                 "attempts_used": f.get("attempts_used", 0)}
                for f in data.get("fixed_phenomena", [])
            ]
            state["rejected_count"] = len(data.get("rejected", []))
        except Exception:  # noqa: BLE001 — 损坏的 fixes.json 也照常归档
            state["status"] = "corrupted"
    runs = ROOT / "data" / "runs"
    if runs.exists():
        state["rounds"] = sum(
            1 for p in runs.iterdir() if p.is_dir() and p.name.startswith("round_")
        )
    return state


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="", help="实验名（归档目录后缀，如 exp_vlm_baseline）")
    parser.add_argument("--dry-run", action="store_true", help="只打印归档计划，不动文件")
    parser.add_argument("--yes", action="store_true", help="跳过确认（play.py 自动调用时使用）")
    args = parser.parse_args()

    seed_orig(args.dry_run)

    existing_dirs = [d for d in ARCHIVE_DIRS if (ROOT / d).exists()
                     and any((ROOT / d).iterdir())]
    existing_files = [f for f in ARCHIVE_FILES if (ROOT / f).exists()]
    if not existing_dirs and not existing_files:
        print("沙盒无实验数据（runs/eval/fixes 均为空），无需归档；游戏已保持原始态。")
        return 0

    state = _collect_state()
    ts = time.strftime("%Y%m%d_%H%M%S")
    name = f"{ts}_{args.tag}" if args.tag else ts

    print(f"归档目标: archives/{name}/")
    print(f"  本轮状态: 修复 {len(state['fixed'])} 项 / 否决记录 {state['rejected_count']} 条"
          f" / {state['rounds']} 局 / status={state['status']}")
    for d in existing_dirs:
        print(f"  - 移动 {d}/")
    for f in existing_files:
        print(f"  - 移动 {f}")
    print("  - 重置 game/tetris_buggy.py ← .orig（12-bug 原始态）")
    print("  - 重置 fixes.json / 工单号清零（回 S0000001）")
    if args.dry_run:
        print("[dry-run] 未执行任何操作。")
        return 0
    if not args.yes:
        resp = input("确认归档并重置？[y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("已取消。")
            return 1

    arch = ROOT / "archives" / name
    arch.mkdir(parents=True)

    # ---- 移动归档 ----
    manifest_files: dict[str, str] = {}
    for rel in existing_files:
        src = ROOT / rel
        dst = arch / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        manifest_files[rel] = _sha256(dst)
    for rel in existing_dirs:
        src = ROOT / rel
        dst = arch / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        src.mkdir(parents=True, exist_ok=True)   # 原位置留空目录

    manifest = {
        "format": "archive-v1",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tag": args.tag,
        "status": state["status"],
        "rounds": state["rounds"],
        "fixed_phenomena": state["fixed"],
        "rejected_count": state["rejected_count"],
        "files_sha256": manifest_files,
    }
    (arch / "archive_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- 重置 ----
    if ORIG_PATH.exists():
        shutil.copyfile(ORIG_PATH, TARGET_PATH)
    if not (ROOT / "data").exists():
        (ROOT / "data").mkdir(parents=True)
    (ROOT / "data" / "fixes.json").write_text(
        json.dumps(EMPTY_FIXES, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # feedback_seq.txt 已随 ARCHIVE_FILES 移走；确认无残留（工单号回 S0000001）
    if FEEDBACK_SEQ.exists():
        FEEDBACK_SEQ.unlink()

    print(f"✔ 已归档 → archives/{name}/（评估报告可用 tetris-game/scripts/score_eval.py --archive 生成）")
    print("✔ 游戏已回退到原始 12-bug 版；fixes.json / 工单号已清零，play.py 下一局从 round_1 开始")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())