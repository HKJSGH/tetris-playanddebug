r"""wrapup — 收尾节点：fixes.json 合并（跨局记忆）+ 尝试历史落盘 + patches.md 渲染 + eval/round_N.json。

唯一允许 LLM 失败容忍的节点：失败改法教训（lesson）属装饰性总结，LLM 任何
失败（连接/解析）都退回纯代码摘要并留痕 errors，绝不中止——此时数据落盘
优先。修复链路上的 LLM 调用仍严格 fail-fast（见各节点 docstring）。
"""
from __future__ import annotations

import json
import time

from agent.config import (
    EVAL_DIR, FIXES_PATH, LESSON_MAX_CHARS, PATCH_HISTORY_PATH, RUNS_ROOT,
    TEXT_MODEL, TEXT_API_KEY_ENV, VISION_MODEL, VISION_API_KEY_ENV,
)
from agent.nodes.common import catalog_by_ph, get_llm_for, TokenDelta
from agent.tools.llm import llm_available, parse_json_text

LESSON_SYSTEM = (
    "你是调试复盘助手。输入是若干个修复失败的假设及各自的补丁尝试记录"
    "（每次尝试的失败原因与替换片段首行）。请为每个假设总结一条「失败改法教训」："
    "这些尝试共同的方向是什么、为什么走不通，一句话（不超过 60 字），"
    "供后续诊断与修复避免重复相同方向。只依据给定材料归纳。"
    '只输出 JSON：{"<假设问题文本>": "教训"}'
)


def _load_fixes() -> dict:
    if FIXES_PATH.exists():
        return json.loads(FIXES_PATH.read_text(encoding="utf-8"))
    return {"version": 1, "fixed_phenomena": [], "rejected": []}


def _append_patch_history(state: dict) -> int:
    """黑板 attempt_log → data/patch_history.jsonl（append-only 审计账本）。

    每条 = 一次补丁尝试的完整记录（含 blocks 全文与失败原因），供事后逐条
    审计「LLM 每次试图改什么、为什么失败」；绝不回流任何 LLM prompt。
    """
    entries = list(state.get("attempt_log") or [])
    if not entries:
        return 0
    PATCH_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PATCH_HISTORY_PATH.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return len(entries)


def _load_history_round(round_id: int) -> list[dict]:
    """读取账本中该局的全部条目（append-only，同 round_id 重跑会累积）。"""
    if not PATCH_HISTORY_PATH.exists():
        return []
    out = []
    for line in PATCH_HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("round_id") == round_id:
            out.append(e)
    return out


def _render_entry_md(e: dict, total: int) -> str:
    """单次尝试 → markdown 段落（失败原因 + 被改函数前后快照）。"""
    ok = bool(e.get("ok"))
    mark = "✅" if ok else "❌"
    lines = [
        f"## 尝试 {e.get('attempt', '?')}/{total} — {mark}（stage: {e.get('stage', '?')}）",
        "",
        f"- 时间：{e.get('ts', '')}　模式：{e.get('mode', '')}",
        f"- 问题假设：{e.get('problem', '') or '（无）'}",
        f"- 嫌疑函数：{e.get('suspect_function', '') or '（无）'}",
    ]
    if e.get("patch_error"):
        lines.append(f"- 补丁产出错误：{e['patch_error']}")
    lines.append(f"- 结果：{'修复成功，已归因 ' + str(e.get('error', '')) if ok else '失败 —— ' + str(e.get('error', ''))}")
    lines.append("")

    snaps = e.get("func_snapshots") or []
    if snaps:
        for s in snaps:
            lines.append(f"### 函数 `{s['function']}`")
            lines.append("")
            lines.append("**补丁前**：")
            lines.append("")
            lines.append("```python")
            lines.append(s.get("before", "（未能捕获）").rstrip())
            lines.append("```")
            lines.append("")
            lines.append("**补丁后**：")
            lines.append("")
            lines.append("```python")
            lines.append(s.get("after", "（未能捕获）").rstrip())
            lines.append("```")
            lines.append("")
    else:
        # 旧账本条目（无函数快照）：退化为 SEARCH/REPLACE 片段展示
        for i, b in enumerate(e.get("blocks") or [], 1):
            lines.append(f"### 补丁块 {i}（旧条目，无函数快照）")
            lines.append("")
            lines.append("**SEARCH（锚定片段）**：")
            lines.append("")
            lines.append("```python")
            lines.append(str(b.get("search", "")).rstrip())
            lines.append("```")
            lines.append("")
            lines.append("**REPLACE（替换为）**：")
            lines.append("")
            lines.append("```python")
            lines.append(str(b.get("replace", "")).rstrip())
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def render_round_patches(round_id: int):
    """该局账本条目 → data/runs/round_N/patches.md（人工 debug 阅读用）。

    幂等：按 round_id 全量过滤重渲染，重跑同局不产生重复段落；
    只含该局条目，绝不回流任何 LLM prompt。
    """
    entries = _load_history_round(round_id)
    if not entries:
        return None
    header = [
        f"# 补丁尝试历史 — round {round_id}",
        "",
        f"目标文件：`game/tetris_buggy.py` · 共 {len(entries)} 次尝试"
        f" · 成功 {sum(1 for e in entries if e.get('ok'))} 次"
        f" · 生成于 {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    md = "\n".join(header) + "\n" + "\n\n".join(
        _render_entry_md(e, len(entries)) for e in entries
    )
    out = RUNS_ROOT / f"round_{round_id}" / "patches.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    return out


def _lesson_fallback(problem: str, attempt_log: list[dict]) -> str:
    """纯代码降级摘要：该问题历次替换片段首行去重拼接（无 LLM / LLM 失败时保底）。"""
    heads: list[str] = []
    for a in attempt_log:
        if str(a.get("problem", "")).strip() != problem or a.get("ok"):
            continue
        for b in a.get("blocks") or []:
            lines = str(b.get("replace", "")).strip().splitlines()
            if lines:
                head = lines[0].strip()
                if head and head not in heads:
                    heads.append(head)
    if not heads:
        return ""
    return ("历次替换片段: " + "；".join(heads[:3]))[:LESSON_MAX_CHARS]


def _attach_lessons(state: dict, rejected: list[dict], acc: TokenDelta) -> list[dict]:
    """失败改法教训（lesson）：每个被否决假设的失败方向一句话语义总结。

    fail-fast 例外（有意设计，见模块 docstring）：LLM 连接/解析失败一律
    退回纯代码摘要并留痕 errors，不中止。教训随 rejected 进 fixes.json，
    下一局经 ingest.rejected_prior 注入诊断 prompt（带条数与长度上限）。
    """
    if not rejected:
        return rejected
    attempt_log = state.get("attempt_log") or []
    records: dict[str, list[dict]] = {}
    for r in rejected:
        p = str(r.get("problem", "")).strip()
        if not p:
            continue
        atts = []
        for a in attempt_log:
            if str(a.get("problem", "")).strip() != p or a.get("ok"):
                continue
            rep_head = next((str(b.get("replace", "")).strip().splitlines()[0][:80]
                             for b in a.get("blocks") or []
                             if str(b.get("replace", "")).strip()), "")
            atts.append({
                "attempt": a.get("attempt"),
                "失败原因": str(a.get("error", ""))[:120],
                "替换片段首行": rep_head,
            })
        if atts:
            records[p] = atts

    lessons: dict[str, str] = {}
    if records and state.get("mode", "mock") != "mock" and llm_available("text"):
        try:
            llm = get_llm_for(state.get("mode", "mock"), "text")
            result = llm.chat(
                [{"role": "system", "content": LESSON_SYSTEM},
                 {"role": "user", "content": json.dumps(records, ensure_ascii=False)}],
                temperature=0.2,
            )
            acc.usage("wrapup", result)
            data = parse_json_text(result.text)
            if isinstance(data, dict):
                lessons = {str(k): str(v).strip()[:LESSON_MAX_CHARS]
                           for k, v in data.items() if str(v).strip()}
        except Exception as e:  # noqa: BLE001 — 收尾总结失败不中止（有意设计）
            acc.error(f"wrapup: 失败改法总结 LLM 失败，退回纯代码摘要 {e!r:.80}")

    out = []
    for r in rejected:
        p = str(r.get("problem", "")).strip()
        lesson = lessons.get(p) or _lesson_fallback(p, attempt_log)
        out.append({**r, "lesson": lesson} if lesson else r)
    return out


def node_wrapup(state: dict) -> dict:
    acc = TokenDelta()
    acc.step("wrapup")
    tokens = {**state.get("tokens", {}), "n_graph_steps": state.get("tokens", {}).get("n_graph_steps", 0)}
    round_id = state.get("round_id", 0)
    catalog = catalog_by_ph()
    fixed = list(state.get("fixed_phenomena") or [])
    rejected = _attach_lessons(state, list(state.get("rejected") or []), acc)
    n_attempts_logged = _append_patch_history(state)
    render_round_patches(round_id)   # 同步渲染 patches.md（人工审计可读版）

    # ---- fixes.json（跨局累积，phenomenon_id 去重） ----
    data = _load_fixes()
    known_fixed = {f["phenomenon_id"] for f in data["fixed_phenomena"]}
    for f in fixed:
        if f["phenomenon_id"] not in known_fixed:
            data["fixed_phenomena"].append(f)
            known_fixed.add(f["phenomenon_id"])
    known_rej = {(r.get("problem"), r.get("round_id")) for r in data.get("rejected", [])}
    for r in rejected:
        r = {**r, "round_id": round_id}
        if (r.get("problem"), round_id) not in known_rej:
            data.setdefault("rejected", []).append(r)
            known_rej.add((r.get("problem"), round_id))
    remaining = sorted(set(catalog) - known_fixed)
    data["remaining"] = remaining
    data["status"] = "converged" if not remaining else "open"
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    FIXES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- eval/round_N.json ----
    hypotheses = state.get("hypotheses") or []
    rejected_problems = {str(r.get("problem", "")).strip() for r in rejected}
    fixed_problems = {str(f.get("hypothesis", "")).strip() for f in fixed}
    # PH 反查（框架内部账本 → eval 纯代码传递，不进任何 prompt）：
    # fixed 从 fixed_phenomena（tester 归因产物）按 problem 文本反查；
    # rejected 直接取 tester 附的 phenomenon_id（无探针来源则空）。
    ph_by_problem = {
        **{str(f.get("hypothesis", "")).strip(): f.get("phenomenon_id", "") for f in fixed},
        **{str(r.get("problem", "")).strip(): r.get("phenomenon_id", "") for r in rejected},
    }
    attempts_by_problem = {
        **{str(r.get("problem", "")).strip(): r.get("attempts", 0) for r in rejected},
        **{str(f.get("hypothesis", "")).strip(): f.get("attempts_used", 0) for f in fixed},
    }
    hyp_rows = []
    for i, h in enumerate(hypotheses, start=1):
        problem = str(h.get("problem", "")).strip()
        outcome = (
            "fixed" if problem and problem in fixed_problems
            else "rejected" if problem and problem in rejected_problems
            else "deferred"
        )
        hyp_rows.append({
            "rank": i,
            "problem": problem,
            "phenomenon_id": ph_by_problem.get(problem, ""),
            "confidence": h.get("confidence"),
            "source": h.get("source", ""),
            "outcome": outcome,
            "attempts": attempts_by_problem.get(problem, 0) if outcome != "deferred" else 0,
        })
    report = state.get("probe_report") or {}
    meta = state.get("round_data").meta if state.get("round_data") else {}
    eval_row = {
        "round_id": round_id,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": state.get("mode", "mock"),
        "models": {
            "vision": VISION_MODEL if llm_available("vision") else "mock",
            "text": TEXT_MODEL if llm_available("text") else "mock",
        },
        "input": {
            "n_events": report.get("n_events", 0),
            "n_screenshots": len(getattr(state.get("round_data"), "screenshots", []) or []),
            "tickets": meta.get("tickets", []),
            "outcome": meta.get("outcome", ""),
        },
        "tokens": {
            "n_llm_calls": tokens["n_llm_calls"],
            "prompt_tokens": tokens["prompt_tokens"],
            "completion_tokens": tokens["completion_tokens"],
            # 前缀缓存命中 tokens 与命中率（cached/prompt；provider 不透传时为 0）
            "cached_tokens": tokens.get("cached_tokens", 0),
            "cache_hit_rate": (
                round(tokens.get("cached_tokens", 0) / tokens["prompt_tokens"], 4)
                if tokens["prompt_tokens"] else 0.0
            ),
            "by_agent": tokens["by_agent"],
        },
        "n_graph_steps": tokens["n_graph_steps"],
        "duration_sec": round(time.time() - state.get("start_ts", time.time()), 3),
        "hypotheses": hyp_rows,
        "hit_summary": {
            "proposed": len(hyp_rows),
            "confirmed": len(fixed),
            "false_positive": len(rejected),
        },
        # tester 跨尝试累积的真实回归清单（非空即修坏过旧账）
        "regressions": len(state.get("regressions") or []),
        # bonus_findings = deferred 假设（提出未走完验证）+ 优化建议旁路
        # 收编的玩家建议（feedback 分拣 kind=suggestion，不进修复循环）
        "bonus_findings": (
            [h["problem"] for h in hyp_rows if h["outcome"] == "deferred"]
            + [f"[玩家建议] {str(s.get('text', '')).strip()}"
               for s in state.get("feedback_symptoms") or []
               if s.get("kind") == "suggestion" and str(s.get("text", "")).strip()]
        ),
        "fixes_applied": len(fixed),
        "cumulative_fixed": len(known_fixed),
        "status": data["status"],
        # 本局补丁尝试明细条数（完整记录 → data/patch_history.jsonl）
        "patch_attempts_logged": n_attempts_logged,
        "errors": tokens.get("errors", []),
    }
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / f"round_{round_id}.json").write_text(
        json.dumps(eval_row, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {"finished": True}
