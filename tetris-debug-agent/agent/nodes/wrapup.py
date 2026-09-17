r"""wrapup — 纯代码节点：合并 fixes.json（跨局记忆）+ 写 eval/round_N.json。"""
from __future__ import annotations

import json
import time

from agent.config import (
    EVAL_DIR, FIXES_PATH, TEXT_MODEL, TEXT_API_KEY_ENV, VISION_MODEL, VISION_API_KEY_ENV,
)
from agent.nodes.common import catalog_by_ph, TokenDelta
from agent.tools.llm import llm_available


def _load_fixes() -> dict:
    if FIXES_PATH.exists():
        return json.loads(FIXES_PATH.read_text(encoding="utf-8"))
    return {"version": 1, "fixed_phenomena": [], "rejected": []}


def node_wrapup(state: dict) -> dict:
    acc = TokenDelta()
    acc.step("wrapup")
    tokens = {**state.get("tokens", {}), "n_graph_steps": state.get("tokens", {}).get("n_graph_steps", 0)}
    round_id = state.get("round_id", 0)
    catalog = catalog_by_ph()
    fixed = list(state.get("fixed_phenomena") or [])
    rejected = list(state.get("rejected") or [])

    # ---- fixes.json（跨局累积，phenomenon_id 去重） ----
    data = _load_fixes()
    known_fixed = {f["phenomenon_id"] for f in data["fixed_phenomena"]}
    for f in fixed:
        if f["phenomenon_id"] not in known_fixed:
            data["fixed_phenomena"].append(f)
            known_fixed.add(f["phenomenon_id"])
    known_rej = {(r["phenomenon_id"], r.get("round_id")) for r in data.get("rejected", [])}
    for r in rejected:
        r = {**r, "round_id": round_id}
        if (r["phenomenon_id"], round_id) not in known_rej:
            data.setdefault("rejected", []).append(r)
            known_rej.add((r["phenomenon_id"], round_id))
    remaining = sorted(set(catalog) - known_fixed)
    data["remaining"] = remaining
    data["status"] = "converged" if not remaining else "open"
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    FIXES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- eval/round_N.json ----
    hypotheses = state.get("hypotheses") or []
    rejected_ph = {r["phenomenon_id"] for r in rejected}
    fixed_ph = {f["phenomenon_id"] for f in fixed}
    attempts_by_ph = {
        **{r["phenomenon_id"]: r.get("attempts", 0) for r in rejected},
        **{f["phenomenon_id"]: f.get("attempts_used", 0) for f in fixed},
    }
    hyp_rows = []
    for i, h in enumerate(hypotheses, start=1):
        outcome = (
            "fixed" if h["phenomenon_id"] in fixed_ph
            else "rejected" if h["phenomenon_id"] in rejected_ph
            else "deferred"
        )
        hyp_rows.append({
            "rank": i,
            "phenomenon_id": h["phenomenon_id"],
            "confidence": h.get("confidence"),
            "source": h.get("source", ""),
            "outcome": outcome,
            "attempts": attempts_by_ph.get(h["phenomenon_id"], 0) if outcome != "deferred" else 0,
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
        "regressions": 0,
        "bonus_findings": [],
        "fixes_applied": len(fixed),
        "cumulative_fixed": len(known_fixed),
        "status": data["status"],
        "errors": tokens.get("errors", []),
    }
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    (EVAL_DIR / f"round_{round_id}.json").write_text(
        json.dumps(eval_row, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {"finished": True}
