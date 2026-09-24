r"""run_pipeline — 单局 / campaign 入口。

单局：python scripts/run_pipeline.py --round 1 [--mode mock|llm] [--verbose]
      [--max-patch-attempts 3] [--max-hypotheses 5]
战役：python scripts/run_pipeline.py --campaign [--rounds 20] [--replay] [--verbose]
      按 fixes.json 逐局推进（round_id 递增），至 converged 或局数上限；
      已有 eval/round_N.json 的局视为完成，跳过（--replay 强制重跑）。

--verbose：控制台逐节点打印黑板变化。无论是否 --verbose，每局的完整黑板流
（节点写入的 state 键 + 产物摘要）与本局摘要都落盘到 logs/round_N_<mode>_<时间>.log。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import (  # noqa: E402
    MAX_HYPOTHESES,
    MAX_PATCH_ATTEMPTS,
    RUNS_ROOT,
    SANDBOX_ROOT,
)
from agent.graph import build_graph  # noqa: E402
from agent.state import merge_tokens  # noqa: E402

EVAL_DIR = SANDBOX_ROOT / "eval"
LOGS_DIR = SANDBOX_ROOT / "logs"
FIXES_PATH = SANDBOX_ROOT / "data" / "fixes.json"
GAME_FILE = SANDBOX_ROOT / "game" / "tetris_buggy.py"


def _read_fixed() -> list[dict]:
    if FIXES_PATH.exists():
        return json.loads(FIXES_PATH.read_text(encoding="utf-8")).get("fixed_phenomena", [])
    return []


def _load_clues() -> dict[str, str]:
    """catalog.yaml 现象号 → 注释线索（总结兜底描述；catalog 是 pipeline 合法输入）。"""
    try:
        from agent.nodes.common import load_catalog
        cat = load_catalog() or {}
    except Exception:  # noqa: BLE001 — catalog 缺失/损坏时总结退化为占位描述
        return {}
    return {p.get("id", ""): str(p.get("clue", "")).strip()
            for p in cat.get("phenomena", []) if p.get("id")}


def print_fix_report(before_ph: set[str], session_rounds: list[int]) -> None:
    """debug 收尾总结：只写现象描述，不暴露 PH/Bug 编号——真实环境下 agent
    不知道现象对应哪个预设 bug，编号仅作内部 key 使用。"""
    clues = _load_clues()
    fx = json.loads(FIXES_PATH.read_text(encoding="utf-8")) if FIXES_PATH.exists() else {}
    fixed = fx.get("fixed_phenomena", [])
    fixed_hyp = {f["phenomenon_id"]: str(f.get("hypothesis", "")).strip() for f in fixed}
    fixed_ids = set(fixed_hyp)
    session_fixed = [ph for ph in fixed_hyp if ph not in before_ph]

    def desc(ph: str) -> str:
        # 已修复条目优先用诊断师归纳的现象描述（hypothesis），其余退回注释线索
        return fixed_hyp.get(ph) or clues.get(ph, "") or "（未登记现象）"

    seen_attempts: dict[str, int] = {}
    for r in fx.get("rejected", []):
        if r.get("round_id") in session_rounds:
            ph = r["phenomenon_id"]
            seen_attempts[ph] = max(seen_attempts.get(ph, 0), r.get("attempts", 0) or 0)

    unresolved = [ph for ph in clues if ph not in fixed_ids and ph not in seen_attempts]

    print("=" * 60)
    print("本次 debug 总结")
    if session_fixed:
        print("✔ 本次确认存在并已修复的问题：")
        for ph in session_fixed:
            print(f"  - {desc(ph)}")
    else:
        print("✔ 本次确认存在并已修复的问题：无")
    if before_ph:
        print("↩ 此前已修复（本次无需重复处理）的问题：")
        for ph in sorted(before_ph):
            print(f"  - {desc(ph)}")
    if seen_attempts:
        print("✘ 本次已尝试修复但未通过测试验证（证据不足或补丁未达标）的问题：")
        for ph, n in seen_attempts.items():
            print(f"  - {desc(ph)}（尝试 {n} 次补丁）")
    if unresolved:
        print("？暂未找到充分证据、本次未能定位修复的问题：")
        for ph in unresolved:
            print(f"  - {desc(ph)}")
    print(f"累计已修复 {len(fixed_ids)} 项 bug")
    print("=" * 60)


def _commit_fixes(before_ph: set[str], round_id: int, mode: str) -> None:
    """本局产生新修复时，把 game/tetris_buggy.py 提交为一个修复快照。

    失败只警告不中断（评估与 fixes.json 已落盘，提交是增量留痕）。
    """
    new = [f for f in _read_fixed() if f["phenomenon_id"] not in before_ph]
    if not new:
        return
    try:
        diff = subprocess.run(
            ["git", "diff", "--quiet", "--", str(GAME_FILE)],
            cwd=SANDBOX_ROOT, capture_output=True,
        )
        if diff.returncode == 0:
            print("  (git) game/tetris_buggy.py 无变更，跳过提交")
            return
        ph_ids = ",".join(f["phenomenon_id"] for f in new)
        body = "\n".join(
            f"- {f['phenomenon_id']}: suspect={f.get('suspect_function') or '?'}, "
            f"attempts={f.get('attempts_used', '?')}"
            for f in new
        )
        msg = f"fix({ph_ids}): agent 修复 {len(new)} 项 (round {round_id}, mode={mode})\n\n{body}"
        subprocess.run(["git", "add", str(GAME_FILE)], cwd=SANDBOX_ROOT, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", msg], cwd=SANDBOX_ROOT, check=True, capture_output=True)
        print(f"  (git) 已提交修复快照: fix({ph_ids}) round {round_id}")
    except Exception as e:  # noqa: BLE001
        print(f"  (git) 提交失败（不影响本局结果）: {e!r:.120}")


def _trunc(v, n: int = 200) -> str:
    s = str(v).replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


def _digest(node: str, delta: dict) -> str:
    """节点产物 → 一行人读摘要（黑板日志用）。"""
    if node == "ingest":
        report = delta.get("probe_report") or {}
        probes = report.get("probes", {})
        sig = [k for k, v in probes.items() if v.get("status") == "signal"]
        fx = delta.get("fixed_prior") or []
        rj = delta.get("rejected_prior") or []
        return (f"事件数={report.get('n_events', '?')} 探针signal={sig or '无'} "
                f"跨局记忆: 已修复={fx or '无'} 已否决={rj or '无'}")
    if node == "vision":
        n = len(delta.get("vision_findings") or [])
        extra = "（vision_skipped）" if delta.get("vision_skipped") else ""
        return f"视觉发现 {n} 条{extra}"
    if node == "feedback":
        n = len(delta.get("feedback_symptoms") or [])
        return f"玩家症状 {n} 条"
    if node == "diagnostician":
        hyps = delta.get("hypotheses")
        if hyps is not None:
            rows = [f"{h['phenomenon_id']}({h.get('suspect_function') or '未知函数'},"
                    f"conf={h.get('confidence')})" for h in hyps]
            return f"假设清单 {len(hyps)} 条: {', '.join(rows) or '空'}"
        cur = delta.get("current_hypothesis")
        return f"切换下一假设: {cur['phenomenon_id'] if cur else '无'}"
    if node == "patcher":
        blocks = (delta.get("patch") or {}).get("blocks") or []
        first = _trunc(blocks[0].get("search", ""), 60) if blocks else "无"
        return f"补丁块 {len(blocks)} 个, 首块 search={first!r}"
    if node == "tester":
        tr = delta.get("test_result") or {}
        line = f"测试[{tr.get('stage', '?')}] passed={tr.get('passed', '-')} failed={tr.get('failed', '-')}"
        if delta.get("fixed_phenomena"):
            line += f" → ✔ 修复{[f['phenomenon_id'] for f in delta['fixed_phenomena']]}"
        if delta.get("rejected"):
            line += f" → ✘ 否决{[r['phenomenon_id'] for r in delta['rejected']]}"
        if tr.get("error"):
            line += f" err={_trunc(tr['error'], 100)!r}"
        return line
    if node == "wrapup":
        return "fixes.json + eval/round 已落盘"
    return "写键: " + ", ".join(sorted(k for k in delta if k != "tokens"))


def run_round(app, round_id: int, mode: str, max_patch_attempts: int,
              max_hypotheses: int, verbose: bool = False) -> tuple[dict, Path]:
    """跑一局。黑板流始终写日志文件；echo=verbose 决定是否同时打印到控制台。"""
    state = {
        "round_id": round_id,
        "run_dir": str(RUNS_ROOT / f"round_{round_id}"),
        "mode": mode,
        "max_patch_attempts": max_patch_attempts,
        "max_hypotheses": max_hypotheses,
        "start_ts": time.time(),
    }
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"round_{round_id}_{mode}_{time.strftime('%Y%m%d_%H%M%S')}.log"
    fh = open(log_path, "w", encoding="utf-8")

    def log(msg: str, echo: bool = False) -> None:
        fh.write(msg + "\n")
        fh.flush()
        if echo:
            print(msg)

    log(f"# round {round_id}  mode={mode}  {time.strftime('%Y-%m-%d %H:%M:%S')}  日志={log_path.name}")

    final: dict = dict(state)
    for chunk in app.stream(state, {"recursion_limit": 100}, stream_mode="updates"):
        if not isinstance(chunk, dict):
            continue
        for node, delta in chunk.items():
            if node == "__end__":
                continue
            tokens_in = (delta or {}).get("tokens")
            if tokens_in:
                delta = {k: v for k, v in (delta or {}).items() if k != "tokens"}
                final["tokens"] = merge_tokens(final.get("tokens"), tokens_in)
            final.update(delta or {})
            log(f"  [{node}] {_digest(node, delta or {})}", echo=verbose)
    return final, log_path


def _eval_row(round_id: int) -> dict:
    p = EVAL_DIR / f"round_{round_id}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def print_summary(final: dict) -> None:
    print("=" * 60)
    print(f"局 {final.get('round_id')}  模式 {final.get('mode')}")
    print(f"图步数 {final['tokens']['n_graph_steps']}  LLM 调用 {final['tokens']['n_llm_calls']}")
    print("假设清单:")
    for h in final.get("hypotheses") or []:
        print(f"  {h.get('hypothesis_id', '?')} {h['phenomenon_id']} conf={h['confidence']} src={h.get('source')}")
    for f in final.get("fixed_phenomena") or []:
        print(f"  ✔ 修复 {f['phenomenon_id']}（suspect={f.get('suspect_function')}, attempts={f['attempts_used']}）")
    for r in final.get("rejected") or []:
        print(f"  ✘ 否决 {r['phenomenon_id']}（{r.get('last_error', '')[:60]}）")
    eval_path = EVAL_DIR / f"round_{final.get('round_id')}.json"
    print(f"eval: {eval_path}")
    print("=" * 60)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", type=int)
    parser.add_argument("--mode", choices=["mock", "llm"], default="mock")
    parser.add_argument("--max-patch-attempts", type=int, default=MAX_PATCH_ATTEMPTS,
                        help="单假设补丁重试上限（缺省读 agent/config.py）")
    parser.add_argument("--max-hypotheses", type=int, default=MAX_HYPOTHESES,
                        help="每局最多推进的假设数（缺省读 agent/config.py）")
    parser.add_argument("--campaign", action="store_true")
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--replay", action="store_true", help="忽略已存在的 eval 记录，强制重跑")
    parser.add_argument("--commit", action="store_true",
                        help="每局产生新修复后，自动 git commit game/tetris_buggy.py 作为修复快照")
    parser.add_argument("--verbose", action="store_true", help="控制台逐节点打印黑板流（日志文件不受影响）")
    args = parser.parse_args()

    app = build_graph()

    if not args.campaign:
        if args.round is None:
            parser.error("单局模式需要 --round N")
        before_ph = {f["phenomenon_id"] for f in _read_fixed()}
        final, log_path = run_round(app, args.round, args.mode, args.max_patch_attempts,
                                    args.max_hypotheses, args.verbose)
        print_summary(final)
        print(f"黑板日志: {log_path}")
        if args.commit:
            _commit_fixes(before_ph, args.round, args.mode)
        print_fix_report(before_ph, [args.round])
        return 0

    # campaign：从 round_1 起逐局；eval 已存在的局跳过（续跑语义），fixes.json
    # status==converged 或局数用尽/无数据即停
    summary = []
    session_rounds: list[int] = []
    before_all = {f["phenomenon_id"] for f in _read_fixed()}
    for rid in range(1, args.rounds + 1):
        run_dir = RUNS_ROOT / f"round_{rid}"
        if not run_dir.exists():
            print(f"round_{rid} 无数据，campaign 停止")
            break
        if (EVAL_DIR / f"round_{rid}.json").exists() and not args.replay:
            print(f"round_{rid} 已有 eval 记录，跳过（--replay 可重跑）")
            continue
        before_ph = {f["phenomenon_id"] for f in _read_fixed()}
        session_rounds.append(rid)
        final, log_path = run_round(app, rid, args.mode, args.max_patch_attempts,
                                    args.max_hypotheses, args.verbose)
        print_summary(final)
        print(f"黑板日志: {log_path}")
        if args.commit:
            _commit_fixes(before_ph, rid, args.mode)
        row = _eval_row(rid)
        summary.append({
            "round_id": rid,
            "mode": row.get("mode", args.mode),
            "fixes_applied": row.get("fixes_applied", 0),
            "cumulative_fixed": row.get("cumulative_fixed", 0),
            "status": row.get("status", "open"),
            "n_llm_calls": (row.get("tokens") or {}).get("n_llm_calls", 0),
            "prompt_tokens": (row.get("tokens") or {}).get("prompt_tokens", 0),
            "completion_tokens": (row.get("tokens") or {}).get("completion_tokens", 0),
            "n_graph_steps": row.get("n_graph_steps", 0),
            "duration_sec": row.get("duration_sec", 0.0),
        })
        fixes_path = SANDBOX_ROOT / "data" / "fixes.json"
        if fixes_path.exists() and json.loads(fixes_path.read_text(encoding="utf-8")).get("status") == "converged":
            print(f"第 {rid} 局达成收敛")
            break
    print_fix_report(before_all, session_rounds)
    if summary:
        EVAL_DIR.mkdir(parents=True, exist_ok=True)
        (EVAL_DIR / "campaign.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"campaign 汇总 → {EVAL_DIR / 'campaign.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
