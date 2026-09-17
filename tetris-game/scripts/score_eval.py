r"""score_eval — 出题方侧离线评分：join truth_map，输出每局汇总与收敛曲线。

数据源（只读）：
  沙盒 eval/round_N.json   —— pipeline 每局评估记录
  沙盒 data/fixes.json     —— 跨局修复记忆（status / remaining）
  本侧 data/gold/truth_map.json —— PH→bug_id 映射（仅离线评估可读，绝不进 prompt）

用法：python scripts/score_eval.py [--sandbox ../tetris-debug-agent] [--out report.md]
零第三方依赖；--out 同时把 Markdown 报告写到指定路径。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOTAL_BUGS = 12


def _load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", default=str(HERE.parent.parent / "tetris-debug-agent"),
                        help="agent 沙盒目录（默认 ../tetris-debug-agent）")
    parser.add_argument("--truth-map", default=None,
                        help="PH→B 映射路径（默认 沙盒/data/gold/truth_map.json）")
    parser.add_argument("--out", help="Markdown 报告输出路径（可选）")
    args = parser.parse_args()

    sandbox = Path(args.sandbox)
    eval_dir = sandbox / "eval"
    truth_path = Path(args.truth_map) if args.truth_map else sandbox / "data" / "gold" / "truth_map.json"
    truth = _load_json(truth_path)
    fixes = _load_json(sandbox / "data" / "fixes.json")

    rows = []
    for p in eval_dir.glob("round_*.json"):
        m = re.fullmatch(r"round_(\d+)", p.stem)
        if m:
            rows.append((int(m.group(1)), _load_json(p)))
    rows.sort(key=lambda t: t[0])

    lines: list[str] = []

    def emit(s: str = "") -> None:
        print(s)
        lines.append(s)

    if not rows:
        emit("无 eval/round_N.json 数据。")
        return 1

    emit(f"# 俄罗斯方块 debug 战役评分报告（{rows[-1][1].get('timestamp', '')[:10]}）")
    emit()
    emit("## 每局汇总")
    emit()
    emit("| 局 | 模式 | 确认修复(PH→B) | 提出 | 误报 | 累计 | LLM调用 | tokens(入/出) | 图步 | 时长s |")
    emit("|---:|---|---|---:|---:|---:|---:|---|---:|---:|")
    tot = {"llm": 0, "p_tok": 0, "c_tok": 0, "steps": 0, "dur": 0.0, "fix": 0, "fp": 0}
    for rid, r in rows:
        toks = r.get("tokens") or {}
        hit = r.get("hit_summary") or {}
        confirmed_ph = [h["phenomenon_id"] for h in r.get("hypotheses", []) if h.get("outcome") == "fixed"]
        confirmed = ", ".join(f"{ph}→{truth.get(ph, '?')}" for ph in confirmed_ph) or "-"
        tot["llm"] += toks.get("n_llm_calls", 0)
        tot["p_tok"] += toks.get("prompt_tokens", 0)
        tot["c_tok"] += toks.get("completion_tokens", 0)
        tot["steps"] += r.get("n_graph_steps", 0)
        tot["dur"] += r.get("duration_sec", 0.0)
        tot["fix"] += r.get("fixes_applied", 0)
        tot["fp"] += hit.get("false_positive", 0)
        emit(f"| {rid} | {r.get('mode', '?')} | {confirmed} | {hit.get('proposed', 0)} "
             f"| {hit.get('false_positive', 0)} | {r.get('cumulative_fixed', 0)} "
             f"| {toks.get('n_llm_calls', 0)} | {toks.get('prompt_tokens', 0)}/{toks.get('completion_tokens', 0)} "
             f"| {r.get('n_graph_steps', 0)} | {r.get('duration_sec', 0.0):.1f} |")

    emit()
    emit("## 收敛曲线（目标 12 bug）")
    emit()
    emit("```")
    for rid, r in rows:
        cum = r.get("cumulative_fixed", 0)
        bar = "█" * cum + "·" * (TOTAL_BUGS - cum)
        emit(f"R{rid:<3}|{bar}| {cum}/{TOTAL_BUGS}")
    emit("```")

    cum_final = rows[-1][1].get("cumulative_fixed", 0)
    n_rounds = len(rows)
    converged = fixes.get("status") == "converged"
    emit()
    emit("## 汇总指标")
    emit()
    emit(f"- 累计修复：{cum_final}/{TOTAL_BUGS}（局数 {n_rounds}，局均 {cum_final / n_rounds:.2f} bug）")
    emit(f"- 收敛状态：{'✔ converged' if converged else '✘ open'}"
         + (f"，第 {rows[-1][0]} 局达成" if converged else f"，remaining={len(fixes.get('remaining', []))}"))
    emit(f"- 假设质量：确认修复 Σ{tot['fix']} / 误报 Σ{tot['fp']}"
         + (f"（命中率 {tot['fix'] / (tot['fix'] + tot['fp']) * 100:.0f}%）" if tot['fix'] + tot['fp'] else ""))
    emit(f"- 成本：LLM 调用 Σ{tot['llm']}，tokens Σ{tot['p_tok']}/{tot['c_tok']}，"
         f"图步 Σ{tot['steps']}，时长 Σ{tot['dur']:.1f}s")
    emit(f"- 回归：Σ{sum(r.get('regressions', 0) for _, r in rows)}")

    # 一致性校验：eval 行合计 vs fixes.json 权威账本
    n_fixed = len(fixes.get("fixed_phenomena", []))
    ok = tot["fix"] == n_fixed
    emit(f"- 一致性：Σ fixes_applied={tot['fix']} vs fixes.json fixed={n_fixed} "
         f"→ {'一致' if ok else '不一致（检查 eval/fixes 落盘）'}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n报告已写 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
