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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import RUNS_ROOT, SANDBOX_ROOT  # noqa: E402
from agent.graph import build_graph  # noqa: E402
from agent.state import merge_tokens  # noqa: E402

EVAL_DIR = SANDBOX_ROOT / "eval"
LOGS_DIR = SANDBOX_ROOT / "logs"


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
    parser.add_argument("--max-patch-attempts", type=int, default=3)
    parser.add_argument("--max-hypotheses", type=int, default=5)
    parser.add_argument("--campaign", action="store_true")
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--replay", action="store_true", help="忽略已存在的 eval 记录，强制重跑")
    parser.add_argument("--verbose", action="store_true", help="控制台逐节点打印黑板流（日志文件不受影响）")
    args = parser.parse_args()

    app = build_graph()

    if not args.campaign:
        if args.round is None:
            parser.error("单局模式需要 --round N")
        final, log_path = run_round(app, args.round, args.mode, args.max_patch_attempts,
                                    args.max_hypotheses, args.verbose)
        print_summary(final)
        print(f"黑板日志: {log_path}")
        return 0

    # campaign：从 round_1 起逐局；eval 已存在的局跳过（续跑语义），fixes.json
    # status==converged 或局数用尽/无数据即停
    summary = []
    for rid in range(1, args.rounds + 1):
        run_dir = RUNS_ROOT / f"round_{rid}"
        if not run_dir.exists():
            print(f"round_{rid} 无数据，campaign 停止")
            break
        if (EVAL_DIR / f"round_{rid}.json").exists() and not args.replay:
            print(f"round_{rid} 已有 eval 记录，跳过（--replay 可重跑）")
            continue
        final, log_path = run_round(app, rid, args.mode, args.max_patch_attempts,
                                    args.max_hypotheses, args.verbose)
        print_summary(final)
        print(f"黑板日志: {log_path}")
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
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / "campaign.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"campaign 汇总 → {EVAL_DIR / 'campaign.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
