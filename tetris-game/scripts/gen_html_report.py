r"""gen_html_report — 出题方侧离线评估的可视化 HTML 报告（自包含单文件，零外部依赖）。

数据源与 score_eval.py 完全一致（直接复用其加载与 PH join 逻辑，只读）：
  沙盒 eval/round_N.json、data/fixes.json、data/catalog/catalog.yaml
  本侧 data/gold/truth_map.json、bugs.yaml

用法：
  python scripts/gen_html_report.py [--sandbox ../tetris-debug-agent] [--out out.html]
  python scripts/gen_html_report.py --archive <归档目录>
--archive 时数据源改为归档目录内 eval/ 与 data/fixes.json；缺省写
沙盒 eval_reports/report.html（归档模式 eval_reports/<归档名>.html）。

产出为内联 CSS/SVG 的自包含 HTML：不依赖网络与第三方前端库，离线可开。
PH 编号/bug 名称仅出现在本报告（出题方侧离线评估），绝不回流 agent。
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from score_eval import (  # noqa: E402
    TOTAL_BUGS, _load_bug_names, _load_channels, _ph_lookup,
)

STATUS_STYLE = {
    "fixed": ("✔ 修复", "st-fixed"),
    "rejected": ("✘ 已尝试未通过", "st-rejected"),
    "proposed": ("？提出未验证", "st-unknown"),
    "unseen": ("？未定位", "st-unknown"),
}


def esc(v) -> str:
    return html.escape(str(v), quote=True)


def _load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _gather(sandbox: Path, fixes_path: Path, truth: dict):
    """eval 行 + fixes 账本 → 报告所需全部派生数据。"""
    eval_dir = sandbox / "eval" if not (sandbox / "eval").exists() else sandbox / "eval"
    fixes = _load_json(fixes_path) if fixes_path.exists() else {}
    rows = []
    for p in sorted(eval_dir.glob("round_*.json")):
        if re.fullmatch(r"round_\d+", p.stem):
            rows.append((int(p.stem.split("_")[1]), _load_json(p)))
    rows.sort(key=lambda t: t[0])
    ph_of = _ph_lookup(fixes)

    fixed_by_ph = {f["phenomenon_id"]: f for f in fixes.get("fixed_phenomena", [])}
    rej_by_ph: dict[str, dict] = {}
    unattributed_rej = 0
    for r in fixes.get("rejected", []):
        ph = str(r.get("phenomenon_id", "") or "")
        if not ph:
            unattributed_rej += 1
            continue
        acc = rej_by_ph.setdefault(ph, {"attempts": 0, "rounds": set()})
        acc["attempts"] += r.get("attempts", 0) or 0
        acc["rounds"].add(r.get("round_id", 0))

    # 12-bug 清单行
    bugs = []
    for ph in sorted(truth):
        bid = truth[ph]
        fx = fixed_by_ph.get(ph)
        rj = rej_by_ph.get(ph)
        if fx:
            st, key = STATUS_STYLE["fixed"]
        elif rj:
            st, key = STATUS_STYLE["rejected"]
        elif any(ph_of(h) == ph for _, r in rows for h in r.get("hypotheses", [])):
            st, key = STATUS_STYLE["proposed"]
        else:
            st, key = STATUS_STYLE["unseen"]
        bugs.append({"ph": ph, "bid": bid, "status": st, "key": key, "fixed": fx, "rej": rj})

    # 每局派生
    rounds = []
    tot = {"llm": 0, "p": 0, "c": 0, "steps": 0, "dur": 0.0, "fix": 0, "fp": 0}
    by_agent: dict[str, int] = {}
    for rid, r in rows:
        toks = r.get("tokens") or {}
        confirmed = sorted({ph_of(h) for h in r.get("hypotheses", [])
                            if h.get("outcome") == "fixed" and ph_of(h)})
        hyp_rows = []
        for h in r.get("hypotheses", []):
            ph = ph_of(h)
            hyp_rows.append({
                "problem": str(h.get("problem", "")).strip(),
                "ph": ph, "bid": truth.get(ph, ""),
                "outcome": h.get("outcome", ""),
                "attempts": h.get("attempts", 0),
                "confidence": h.get("confidence"),
            })
        rounds.append({
            "rid": rid, "mode": r.get("mode", "?"), "confirmed": confirmed,
            "hit": r.get("hit_summary") or {}, "cum": r.get("cumulative_fixed", 0),
            "llm": toks.get("n_llm_calls", 0), "p": toks.get("prompt_tokens", 0),
            "c": toks.get("completion_tokens", 0), "steps": r.get("n_graph_steps", 0),
            "dur": r.get("duration_sec", 0.0), "hyps": hyp_rows,
            "regressions": r.get("regressions", 0), "bonus": r.get("bonus_findings") or [],
            "n_events": (r.get("input") or {}).get("n_events", 0),
        })
        tot["llm"] += toks.get("n_llm_calls", 0)
        tot["p"] += toks.get("prompt_tokens", 0)
        tot["c"] += toks.get("completion_tokens", 0)
        tot["steps"] += r.get("n_graph_steps", 0)
        tot["dur"] += r.get("duration_sec", 0.0)
        tot["fix"] += r.get("fixes_applied", 0)
        tot["fp"] += (r.get("hit_summary") or {}).get("false_positive", 0)
        for ag, v in (toks.get("by_agent") or {}).items():
            by_agent[ag] = by_agent.get(ag, 0) + v.get("prompt_tokens", 0) + v.get("completion_tokens", 0)

    regressions_total = sum(r.get("regressions", 0) for _, r in rows)
    n_fixed = len(fixed_by_ph)
    ok_consistency = tot["fix"] == n_fixed
    return {
        "rows": rows, "fixes": fixes, "bugs": bugs, "rounds": rounds, "tot": tot,
        "by_agent": by_agent, "unattributed_rej": unattributed_rej,
        "cum_final": rows[-1][1].get("cumulative_fixed", 0) if rows else 0,
        "converged": fixes.get("status") == "converged",
        "remaining": fixes.get("remaining", []),
        "regressions_total": regressions_total,
        "consistency_ok": ok_consistency, "n_fixed": n_fixed,
    }


# ---- HTML 片段 ---------------------------------------------------------------

def _svg_convergence(rounds: list[dict]) -> str:
    """累计修复收敛曲线（SVG 阶梯折线，目标 12）。"""
    W, H, ML, MR, MT, MB = 760, 280, 46, 20, 16, 34
    pw, phh = W - ML - MR, H - MT - MB
    n = max(len(rounds), 1)
    xmax = max(n, 8)  # 至少 8 格，曲线短时不至于挤成一团

    def X(i: int) -> float:
        return ML + pw * (i / xmax) if n > 1 else ML + pw / 2

    def Y(v: float) -> float:
        return MT + phh * (1 - v / TOTAL_BUGS)

    grid, labels = [], []
    for v in range(0, TOTAL_BUGS + 1, 3):
        y = Y(v)
        grid.append(f'<line x1="{ML}" y1="{y:.1f}" x2="{W - MR}" y2="{y:.1f}" class="grid"/>')
        labels.append(f'<text x="{ML - 8}" y="{y + 4:.1f}" class="tick" text-anchor="end">{v}</text>')
    # x 轴局号
    xlabels = []
    shown = max(1, n // 12) if n > 12 else 1
    for i, rd in enumerate(rounds):
        if i % shown == 0:
            xlabels.append(f'<text x="{X(i):.1f}" y="{H - MB + 18}" class="tick" text-anchor="middle">R{rd["rid"]}</text>')
    # 阶梯路径：每局的水平段 + 到下一局的竖直段
    pts = []
    for i, rd in enumerate(rounds):
        pts.append(f'{X(i):.1f},{Y(rd["cum"]):.1f}')
        if i + 1 < len(rounds):
            pts.append(f'{X(i + 1):.1f},{Y(rd["cum"]):.1f}')
    poly = " ".join(pts)
    dots = "".join(
        f'<circle cx="{X(i):.1f}" cy="{Y(rd["cum"]):.1f}" r="3.5" class="dot">'
        f'<title>R{rd["rid"]}: {rd["cum"]}/{TOTAL_BUGS}</title></circle>'
        for i, rd in enumerate(rounds))
    target = f'<line x1="{ML}" y1="{Y(TOTAL_BUGS):.1f}" x2="{W - MR}" y2="{Y(TOTAL_BUGS):.1f}" class="target"/>'
    tlabel = f'<text x="{W - MR}" y="{Y(TOTAL_BUGS) - 6:.1f}" class="tick target-t" text-anchor="end">目标 {TOTAL_BUGS}</text>'
    return (f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="收敛曲线">'
            + "".join(grid) + "".join(labels) + target + tlabel
            + f'<polyline points="{poly}" class="conv"/>'
            + dots + "".join(xlabels) + "</svg>")


def _cost_bars(by_agent: dict[str, int]) -> str:
    """成本构成横向条形（tokens 占比，纯 CSS）。"""
    tot = sum(by_agent.values())
    if not tot:
        return '<p class="muted">无 token 数据。</p>'
    mx = max(by_agent.values())
    rows = []
    for ag, v in sorted(by_agent.items(), key=lambda kv: -kv[1]):
        if v <= 0:
            continue
        pct = v / tot * 100
        w = v / mx * 100
        rows.append(
            f'<div class="bar-row"><span class="bar-name">{esc(ag)}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{w:.1f}%"></span></span>'
            f'<span class="bar-val">{pct:.0f}%（{v:,}）</span></div>')
    return "".join(rows)


def _kpi(label: str, value: str, sub: str = "", cls: str = "") -> str:
    return (f'<div class="kpi {cls}"><div class="kpi-v">{value}</div>'
            f'<div class="kpi-l">{esc(label)}</div>'
            + (f'<div class="kpi-s">{esc(sub)}</div>' if sub else "") + "</div>")


def _render(title: str, date: str, d: dict, channels: dict, bug_names: dict) -> str:
    tot = d["tot"]
    n_rounds = len(d["rounds"])
    hit = (f"{tot['fix']} / {tot['fix'] + tot['fp']}"
           + (f"（{tot['fix'] / (tot['fix'] + tot['fp']) * 100:.0f}%）" if tot["fix"] + tot["fp"] else ""))
    conv = "✔ converged" if d["converged"] else f"✘ open（remaining {len(d['remaining'])}）"

    # KPI 卡片
    kpis = "".join([
        _kpi("累计修复", f"{d['cum_final']}<span class='dim'>/{TOTAL_BUGS}</span>",
             f"局数 {n_rounds}，局均 {d['cum_final'] / n_rounds:.2f}" if n_rounds else "",
             "kpi-green" if d["cum_final"] == TOTAL_BUGS else ""),
        _kpi("收敛状态", conv),
        _kpi("假设命中率", hit, f"确认 Σ{tot['fix']} / 误报 Σ{tot['fp']}"),
        _kpi("总成本", f"{tot['llm']} 次调用",
             f"{tot['p'] + tot['c']:,} tokens · {tot['dur']:.0f}s"),
        _kpi("回归", f"Σ{d['regressions_total']}",
             "" if d["regressions_total"] == 0 else "存在修坏旧账！",
             "" if d["regressions_total"] == 0 else "kpi-red"),
    ])

    # 修复时间线
    tl = []
    for rd in d["rounds"]:
        names = ", ".join(f"{b['ph']}→{b['bid']}" for b in d["bugs"] if b["ph"] in rd["confirmed"]) or "—"
        cls = "tl-fix" if rd["confirmed"] else ("tl-stall" if rd["hyps"] else "tl-clean")
        tag = {"tl-fix": "修复", "tl-stall": "受阻", "tl-clean": "clean"}[cls]
        tl.append(f'<div class="tl-row {cls}"><span class="tl-badge">{tag}</span>'
                  f'<span class="tl-rid">R{rd["rid"]}</span>'
                  f'<span class="tl-body">{esc(names)}'
                  f'<span class="dim">（累计 {rd["cum"]}/{TOTAL_BUGS}）</span></span></div>')
    timeline = "".join(tl) or '<p class="muted">无对局数据。</p>'

    # 12-bug 清单
    bug_rows = []
    for b in d["bugs"]:
        fx, rj = b["fixed"], b["rej"]
        if fx:
            detail = f"{(fx or {}).get('attempts_used', 0)} 次补丁"
        elif rj:
            detail = f"{rj['attempts']} 次补丁（局 {','.join(map(str, sorted(rj['rounds'])))}）"
        else:
            detail = "—"
        inv_rounds = ",".join(str(rd["rid"]) for rd in d["rounds"]
                              if any(h["ph"] == b["ph"] and h["outcome"] != "deferred" for h in rd["hyps"])) or "-"
        chs = "/".join(channels.get(b["ph"], [])) or "-"
        bug_rows.append(
            f'<tr><td class="mono">{b["bid"]}</td><td>{esc(bug_names.get(b["bid"], ""))}</td>'
            f'<td><span class="{b["key"]}">{b["status"]}</span></td>'
            f'<td>{esc(inv_rounds)}</td><td>{esc(detail)}</td>'
            f'<td>{esc(chs)}</td><td>{esc((fx or {}).get("suspect_function", "") or "-")}</td></tr>')
    note = (f'<p class="muted">注：{d["unattributed_rej"]} 条已否决假设无探针来源、'
            "无法归因到具体 bug，未计入「已尝试未通过」。</p>" if d["unattributed_rej"] else "")

    # 每局汇总
    round_rows = []
    for rd in d["rounds"]:
        conf = ", ".join(f"{ph}→{next((b['bid'] for b in d['bugs'] if b['ph'] == ph), '?')}"
                         for ph in rd["confirmed"]) or "—"
        round_rows.append(
            f'<tr><td>R{rd["rid"]}</td><td>{esc(rd["mode"])}</td>'
            f'<td class="mono-sm">{esc(conf)}</td>'
            f'<td>{rd["hit"].get("proposed", 0)}</td><td>{rd["hit"].get("false_positive", 0)}</td>'
            f'<td>{rd["cum"]}</td><td>{rd["llm"]}</td>'
            f'<td>{rd["p"]:,}/{rd["c"]:,}</td><td>{rd["steps"]}</td><td>{rd["dur"]:.1f}</td></tr>')

    # 每局假设明细（折叠）
    details = []
    for rd in d["rounds"]:
        if not rd["hyps"]:
            continue
        rows = []
        for h in rd["hyps"]:
            oc = {"fixed": ("✔ 已修复", "st-fixed"), "rejected": ("✘ 未通过", "st-rejected"),
                  "deferred": ("－ 未验证", "st-unknown")}.get(h["outcome"], ("?", "st-unknown"))
            ph = f'{h["ph"]}→{h["bid"]}' if h["ph"] else "未归因"
            rows.append(f'<tr><td>{esc(h["problem"])}</td><td class="mono-sm">{esc(ph)}</td>'
                        f'<td><span class="{oc[1]}">{oc[0]}</span></td>'
                        f'<td>{h["attempts"] or "-"}</td><td>{h["confidence"] if h["confidence"] is not None else "-"}</td></tr>')
        bonus = (f'<p class="muted">deferred：{"；".join(esc(x) for x in rd["bonus"])}</p>'
                 if rd["bonus"] else "")
        details.append(
            f'<details><summary>R{rd["rid"]}（{len(rd["hyps"])} 条假设，'
            f'{rd["llm"]} 次调用）</summary>'
            f'<table class="tbl"><tr><th>问题（自然语言）</th><th>归因</th><th>结果</th><th>尝试</th><th>置信度</th></tr>'
            + "".join(rows) + "</table>" + bonus + "</details>")

    consistency = ("✔ 一致" if d["consistency_ok"]
                   else f"✘ 不一致（Σ fixes_applied={tot['fix']} vs fixes.json fixed={d['n_fixed']}）")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
:root {{ --fg:#1f2328; --muted:#57606a; --line:#d0d7de; --bg:#fff; --bg2:#f6f8fa;
  --green:#1a7f37; --red:#cf222e; --blue:#0969da; --accent:#8250df; }}
* {{ box-sizing:border-box; }}
body {{ font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  color:var(--fg); background:var(--bg); margin:0; padding:32px 16px; line-height:1.55; }}
.wrap {{ max-width:980px; margin:0 auto; }}
h1 {{ font-size:24px; border-bottom:2px solid var(--line); padding-bottom:10px; }}
h2 {{ font-size:18px; margin:34px 0 12px; }}
.date {{ color:var(--muted); font-size:13px; }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; }}
.kpi {{ background:var(--bg2); border:1px solid var(--line); border-radius:8px; padding:12px 14px; }}
.kpi-v {{ font-size:22px; font-weight:600; }}
.kpi-v .dim {{ color:var(--muted); font-size:15px; font-weight:400; }}
.kpi-l {{ font-size:12px; color:var(--muted); margin-top:2px; }}
.kpi-s {{ font-size:11px; color:var(--muted); margin-top:2px; }}
.kpi-green .kpi-v {{ color:var(--green); }}
.kpi-red .kpi-v {{ color:var(--red); }}
.chart {{ width:100%; height:auto; background:var(--bg2); border:1px solid var(--line);
  border-radius:8px; margin-top:8px; }}
.grid {{ stroke:#e1e4e8; stroke-width:1; }}
.conv {{ fill:none; stroke:var(--blue); stroke-width:2.5; }}
.dot {{ fill:var(--blue); }}
.target {{ stroke:var(--red); stroke-width:1; stroke-dasharray:6 4; }}
.target-t {{ fill:var(--red); }}
.tick {{ font-size:11px; fill:var(--muted); }}
.tl-row {{ display:flex; align-items:baseline; gap:10px; padding:6px 0;
  border-bottom:1px dashed var(--line); }}
.tl-badge {{ font-size:11px; padding:1px 8px; border-radius:10px; color:#fff; flex:none; }}
.tl-fix .tl-badge {{ background:var(--green); }}
.tl-stall .tl-badge {{ background:var(--red); }}
.tl-clean .tl-badge {{ background:var(--muted); }}
.tl-rid {{ font-weight:600; flex:none; width:36px; }}
.tbl {{ border-collapse:collapse; width:100%; font-size:13px; margin-top:8px; }}
.tbl th,.tbl td {{ border:1px solid var(--line); padding:6px 9px; text-align:left; }}
.tbl th {{ background:var(--bg2); font-weight:600; }}
.bar-row {{ display:flex; align-items:center; gap:10px; margin:6px 0; }}
.bar-name {{ width:110px; font-size:13px; text-align:right; color:var(--muted); flex:none; }}
.bar-track {{ flex:1; background:var(--bg2); border-radius:6px; height:18px; overflow:hidden; }}
.bar-fill {{ display:block; height:100%; background:linear-gradient(90deg,var(--blue),var(--accent));
  border-radius:6px; }}
.bar-val {{ width:170px; font-size:12px; color:var(--muted); flex:none; }}
.st-fixed {{ color:var(--green); font-weight:600; }}
.st-rejected {{ color:var(--red); font-weight:600; }}
.st-unknown {{ color:var(--muted); }}
.mono {{ font-family:Consolas,monospace; }}
.mono-sm {{ font-family:Consolas,monospace; font-size:12px; }}
.muted {{ color:var(--muted); font-size:12px; }}
.dim {{ color:var(--muted); font-weight:400; }}
details {{ border:1px solid var(--line); border-radius:8px; padding:8px 14px; margin:8px 0;
  background:var(--bg); }}
summary {{ cursor:pointer; font-weight:600; font-size:13px; }}
.ok {{ color:var(--green); }} .bad {{ color:var(--red); }}
footer {{ margin-top:40px; color:var(--muted); font-size:12px;
  border-top:1px solid var(--line); padding-top:12px; }}
</style></head><body><div class="wrap">
<h1>{esc(title)}<span class="date">　{esc(date)}</span></h1>
<div class="kpis">{kpis}</div>

<h2>收敛曲线（目标 {TOTAL_BUGS} bug）</h2>
{_svg_convergence(d["rounds"])}

<h2>修复时间线</h2>
{timeline}

<h2>12-bug 修复清单</h2>
<table class="tbl">
<tr><th>Bug</th><th>名称</th><th>结果</th><th>相关局</th><th>补丁尝试</th><th>证据通道</th><th>嫌疑函数</th></tr>
{"".join(bug_rows)}
</table>
{note}

<h2>每局汇总</h2>
<table class="tbl">
<tr><th>局</th><th>模式</th><th>确认修复(PH→B)</th><th>提出</th><th>误报</th><th>累计</th>
<th>LLM调用</th><th>tokens(入/出)</th><th>图步</th><th>时长s</th></tr>
{"".join(round_rows)}
</table>

<h2>成本构成（tokens，按节点）</h2>
{_cost_bars(d["by_agent"])}

<h2>每局假设明细</h2>
{"".join(details) or '<p class="muted">无假设记录。</p>'}

<h2>数据一致性</h2>
<p class="{'ok' if d['consistency_ok'] else 'bad'}">{consistency}</p>

<footer>由 gen_html_report.py 生成（出题方侧离线评估；PH 编号不回流 agent）。
数据源：eval/round_N.json + data/fixes.json + truth_map（join 逻辑与 score_eval.py 一致）。</footer>
</div></body></html>"""


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", default=str(HERE.parent.parent / "tetris-debug-agent"),
                        help="agent 沙盒目录（默认 ../tetris-debug-agent）")
    parser.add_argument("--archive", default=None,
                        help="archive_reset 归档目录：从归档内 eval/ 与 data/fixes.json 出报告")
    parser.add_argument("--truth-map", default=None, help="PH→B 映射路径")
    parser.add_argument("--out", help="HTML 输出路径（--archive 时缺省写 沙盒 eval_reports/<归档名>.html）")
    args = parser.parse_args()

    sandbox = Path(args.sandbox)
    if args.archive:
        arch = Path(args.archive)
        fixes_path = arch / "data" / "fixes.json"
        default_out = sandbox / "eval_reports" / f"{arch.name}.html"
        title = f"实验报告：{arch.name}"
    else:
        fixes_path = sandbox / "data" / "fixes.json"
        default_out = sandbox / "eval_reports" / "report.html"
        title = "俄罗斯方块 debug 战役评分报告"
    truth_path = Path(args.truth_map) if args.truth_map else sandbox / "data" / "gold" / "truth_map.json"
    truth = _load_json(truth_path)
    channels = _load_channels(sandbox)
    bug_names = _load_bug_names()

    d = _gather(sandbox, fixes_path, truth)
    if not d["rows"]:
        print(f"无 eval 数据（sandbox={sandbox}）。")
        return 1
    date = d["rows"][-1][1].get("timestamp", "")[:10]
    html_text = _render(title, date, d, channels, bug_names)

    out_path = Path(args.out) if args.out else default_out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_text, encoding="utf-8")
    print(f"HTML 报告已写 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
