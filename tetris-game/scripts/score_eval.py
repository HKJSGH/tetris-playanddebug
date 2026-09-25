r"""score_eval — 出题方侧离线评分：join truth_map，输出每局汇总、12-bug 修复清单与收敛曲线。

数据源（只读）：
  沙盒 eval/round_N.json   —— pipeline 每局评估记录
  沙盒 data/fixes.json     —— 跨局修复记忆（status / remaining）
  本侧 data/gold/truth_map.json —— PH→bug_id 映射（仅离线评估可读，绝不进 prompt）
  本侧 bugs.yaml           —— bug 名称（仅报告展示用）
  沙盒 data/catalog/catalog.yaml —— 现象证据通道 observable_via（工具链路展示）

用法：
  python scripts/score_eval.py [--sandbox ../tetris-debug-agent] [--out report.md]
  python scripts/score_eval.py --archive <归档目录>   # 对 archive_reset 归档出实验报告
--archive 时数据源改为归档目录内 eval/ 与 data/fixes.json；报告缺省写入
沙盒 eval_reports/<归档名>.md（该目录入库；archives/ 本体 gitignore）。
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


def _load_channels(sandbox: Path) -> dict[str, list[str]]:
    """catalog.yaml → {PH-xx: [telemetry/feedback/screenshot]}（工具链路展示用）。"""
    text = (sandbox / "data" / "catalog" / "catalog.yaml").read_text(encoding="utf-8")
    try:
        import yaml
        cat = yaml.safe_load(text) or {}
        return {p.get("id", ""): list(p.get("observable_via") or [])
                for p in cat.get("phenomena", []) if p.get("id")}
    except ImportError:
        # 零依赖兜底：按 phenomena 块切分后正则取 observable_via
        out: dict[str, list[str]] = {}
        blocks = re.split(r"(?m)^\s*-\s+id:", text)[1:]
        for b in blocks:
            m_id = re.match(r"\s*(PH-\d+)", b)
            m_ch = re.search(r"observable_via:\s*\[([^\]]*)\]", b)
            if m_id and m_ch:
                out[m_id.group(1)] = [c.strip() for c in m_ch.group(1).split(",")]
        return out


def _load_bug_names() -> dict[str, str]:
    """bugs.yaml → {B: name}（报告展示用；pyyaml 缺失时退化为空表）。"""
    bugs_path = HERE.parent / "bugs.yaml"
    try:
        import yaml
        bugs = yaml.safe_load(bugs_path.read_text(encoding="utf-8")) or {}
        return {bid: str(b.get("name", "")) for bid, b in bugs.items() if isinstance(b, dict)}
    except Exception:  # noqa: BLE001 — 无 pyyaml/文件缺失时清单不显示名称
        return {}


def _checklist(rows, fixes: dict, truth: dict, channels: dict, bug_names: dict, emit) -> None:
    """12-bug 修复清单：每 bug 是否修复 / 工具链路 / 开销。"""
    fixed_by_ph = {f["phenomenon_id"]: f for f in fixes.get("fixed_phenomena", [])}
    rej_rounds: dict[str, list[tuple[int, int]]] = {}
    for r in fixes.get("rejected", []):
        rej_rounds.setdefault(r["phenomenon_id"], []).append(
            (r.get("round_id", 0), r.get("attempts", 0) or 0))

    # 每 PH 的相关局次与开销（该局 eval 假设表里出现过即算相关；多 bug 同局时开销重复计入）
    involved: dict[str, dict] = {}
    for rid, r in rows:
        toks = r.get("tokens") or {}
        cost = (toks.get("n_llm_calls", 0), toks.get("prompt_tokens", 0),
                toks.get("completion_tokens", 0), r.get("n_graph_steps", 0),
                r.get("duration_sec", 0.0))
        for h in r.get("hypotheses", []):
            ph = h.get("phenomenon_id", "")
            inv = involved.setdefault(ph, {"rounds": [], "attempts": 0, "source": ""})
            if rid not in inv["rounds"]:
                inv["rounds"].append(rid)
            inv["attempts"] = max(inv["attempts"], h.get("attempts", 0) or 0)
            if h.get("outcome") != "deferred" or not inv["source"]:
                inv["source"] = h.get("source", "") or inv["source"]
            inv["cost"] = cost
    fixed_rounds = {ph: rid for rid, r in rows
                    for h in r.get("hypotheses", [])
                    if h.get("outcome") == "fixed"
                    for ph in [h.get("phenomenon_id", "")]}

    emit("## 12-bug 修复清单")
    emit()
    emit("| Bug | 名称 | 结果 | 相关局 | 补丁尝试 | 工具链路（证据通道 / 假设来源） | 嫌疑函数 | 相关局开销 调用/tokens(入+出)/步/时长s |")
    emit("|---|---|---|---|---:|---|---|---|")
    for ph in sorted(truth):
        bid = truth[ph]
        name = bug_names.get(bid, "")
        fx = fixed_by_ph.get(ph)
        inv = involved.get(ph, {})
        if fx:
            result = f"✔ 修复（{fixed_rounds.get(ph, '?')} 局确认）"
        elif ph in rej_rounds:
            n = sum(a for _, a in rej_rounds[ph])
            result = f"✘ 已尝试未通过（{n} 次补丁，局 {','.join(map(str, sorted({r for r, _ in rej_rounds[ph]})))}）"
        elif inv:
            result = "？提出假设未验证（证据不足）"
        else:
            result = "？未定位（无假设）"
        chs = "/".join(channels.get(ph, [])) or "-"
        src = {"llm": "LLM 诊断", "fallback": "探针兜底"}.get(inv.get("source", ""), inv.get("source", "") or "-")
        c = inv.get("cost") or (0, 0, 0, 0, 0.0)
        cost = f"{c[0]} / {c[1]}+{c[2]} / {c[3]} / {c[4]:.1f}" if inv else "-"
        emit(f"| {bid} | {name} | {result} | {','.join(map(str, inv.get('rounds', []))) or '-'} "
             f"| {inv.get('attempts', 0) or (fx or {}).get('attempts_used', 0)} | {chs} / {src} "
             f"| {(fx or {}).get('suspect_function', '') or '-'} | {cost} |")
    emit()
    emit("> 开销口径：该 bug 出现在假设清单的局次之整局开销（多 bug 同局时重复计入）；"
         "tokens 为 prompt+completion。")
    emit()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", default=str(HERE.parent.parent / "tetris-debug-agent"),
                        help="agent 沙盒目录（默认 ../tetris-debug-agent）")
    parser.add_argument("--archive", default=None,
                        help="archive_reset 归档目录：从归档内 eval/ 与 data/fixes.json 出实验报告")
    parser.add_argument("--truth-map", default=None,
                        help="PH→B 映射路径（默认 沙盒/data/gold/truth_map.json）")
    parser.add_argument("--out", help="Markdown 报告输出路径（--archive 时缺省写 沙盒 eval_reports/<归档名>.md）")
    args = parser.parse_args()

    sandbox = Path(args.sandbox)
    if args.archive:
        arch = Path(args.archive)
        eval_dir = arch / "eval"
        fixes_path = arch / "data" / "fixes.json"
        default_out = sandbox / "eval_reports" / f"{arch.name}.md"
        header_tag = f"实验报告：{arch.name}"
    else:
        eval_dir = sandbox / "eval"
        fixes_path = sandbox / "data" / "fixes.json"
        default_out = None
        header_tag = "俄罗斯方块 debug 战役评分报告"
    truth_path = Path(args.truth_map) if args.truth_map else sandbox / "data" / "gold" / "truth_map.json"
    truth = _load_json(truth_path)
    fixes = _load_json(fixes_path) if fixes_path.exists() else {}
    channels = _load_channels(sandbox)
    bug_names = _load_bug_names()

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
        emit(f"无 eval 数据（{'归档' if args.archive else '沙盒'}: {eval_dir}）。")
        return 1

    emit(f"# {header_tag}（{rows[-1][1].get('timestamp', '')[:10]}）")
    emit()
    _checklist(rows, fixes, truth, channels, bug_names, emit)
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

    out_path = Path(args.out) if args.out else default_out
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n报告已写 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
