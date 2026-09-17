r"""diagnostician — LLM 节点：三证据 + catalog → 假设清单。

首次进入：生成（或兜底生成）假设清单；再次进入：取下一个假设。
兜底 hypotheses_from_probes 为纯 Python：探针 signal → catalog 现象直接
映射（探针内部编号 B 与 catalog PH 按序一一对应，纯代码路径）。
"""
from __future__ import annotations

import json
import re

from agent.nodes.common import b_to_ph, catalog_by_ph, get_llm_for, TokenDelta
from agent.tools.llm import llm_available, parse_json_text

SYSTEM = (
    "你是俄罗斯方块游戏的诊断专家。根据三路证据（视觉发现、玩家症状、遥测探针）"
    "对照现象目录（catalog：每个现象有 id/name/symptom/signal/observable_via）"
    "提出修复假设清单。\n"
    "约束：\n"
    "1. phenomenon_id 必须取自 catalog；\n"
    "2. suspect_function 必须取自候选函数列表；\n"
    "3. 每个现象只提一个假设，按置信度降序；\n"
    '4. 输出 JSON 数组：[{"phenomenon_id": "PH-xx", "suspect_function": "函数名",'
    ' "confidence": 0-1, "rationale": "理由", "evidence": ["证据要点"]}]\n'
    "5. already_fixed 中的现象已修复，勿再提出；previously_rejected 是此前局被否决的"
    "记录，除非本局证据给出明显新线索，否则不要重复提出。"
    "只依据给定材料，不要臆造现象。"
)


def hypotheses_from_probes(report: dict) -> list[dict]:
    """兜底：探针 signal → catalog 现象直接映射（B 序号 ↔ PH 序号）。"""
    out = []
    for i, (probe_key, pr) in enumerate(report.get("probes", {}).items(), start=1):
        if pr["status"] != "signal":
            continue
        ph = b_to_ph(probe_key)
        out.append({
            "hypothesis_id": f"H{i}",
            "phenomenon_id": ph,
            "suspect_function": "",       # 由 patcher 兜底补齐（全函数扫描）
            "confidence": float(pr.get("confidence", 0.5)),
            "rationale": "探针信号直接映射（纯代码兜底）",
            "evidence": pr.get("evidence", []),
            "source": "fallback",
        })
    out.sort(key=lambda h: -h["confidence"])
    return out


def _norm_ph(raw: str) -> str:
    """容错归一化现象号：'PH-5' / 'ph5' / 'PH-05（预览）' → 'PH-05'。"""
    m = re.search(r"(\d+)", str(raw))
    return f"PH-{int(m.group(1)):02d}" if m else ""


def _norm_fn(raw: str, names: set[str]) -> str:
    """容错归一化函数名：去括号尾部、剥类前缀（TetrisGame._on_right → _on_right）。"""
    fn = str(raw).strip()
    if "(" in fn:
        fn = fn.split("(", 1)[0].strip()
    if fn not in names and "." in fn and fn.rsplit(".", 1)[-1] in names:
        fn = fn.rsplit(".", 1)[-1]
    return fn


def node_diagnostician(state: dict) -> dict:
    acc = TokenDelta()
    acc.step("diagnostician")

    cursor = state.get("hypothesis_cursor", -1)
    hypotheses = state.get("hypotheses") or []

    # 再次进入：推进到下一个假设
    if hypotheses:
        nxt = cursor + 1
        while nxt < len(hypotheses) and hypotheses[nxt]["phenomenon_id"] in {
            f["phenomenon_id"] for f in state.get("fixed_phenomena", [])
        } | {r["phenomenon_id"] for r in state.get("rejected", [])}:
            nxt += 1
        if nxt >= len(hypotheses) or nxt >= state.get("max_hypotheses", 5):
            return {**acc.out(), "current_hypothesis": None, "hypothesis_cursor": nxt}
        return {
            **acc.out(),
            "hypothesis_cursor": nxt,
            "current_hypothesis": hypotheses[nxt],
            "patch_attempts": 0,
        }

    # 首次进入：生成假设
    report = state["probe_report"]
    mode = state.get("mode", "mock")
    catalog = state.get("catalog") or {"phenomena": []}
    srcmap = state.get("srcmap") or {}
    symptoms = state.get("feedback_symptoms") or []
    vision = state.get("vision_findings") or []
    fixed_prior = set(state.get("fixed_prior") or [])
    rejected_prior = set(state.get("rejected_prior") or [])

    if mode == "mock" or not llm_available("text"):
        hyps = [h for h in hypotheses_from_probes(report) if h["phenomenon_id"] not in fixed_prior]
        first = hyps[0] if hyps else None
        return {**acc.out(), "hypotheses": hyps, "hypothesis_cursor": 0 if first else -1,
                "current_hypothesis": first, "patch_attempts": 0}

    llm = get_llm_for(mode, "text")
    signal_probes = {
        k: v for k, v in report.get("probes", {}).items() if v["status"] == "signal"
    }
    # 探针内部编号不进 prompt：signal 证据转 PH 描述
    probe_brief = [
        {"phenomenon": b_to_ph(k), "evidence": v.get("evidence", [])}
        for k, v in signal_probes.items()
    ]
    user = {
        "catalog": catalog["phenomena"],
        "vision_findings": vision or "（无截图或未启用视觉）",
        "player_symptoms": symptoms or "（无）",
        "probe_signals": probe_brief or "（无）",
        "candidate_functions": sorted(srcmap.keys()),
        "already_fixed": sorted(fixed_prior) or "（无）",
        "previously_rejected": sorted(rejected_prior) or "（无）",
    }
    try:
        result = llm.chat(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
            temperature=0.2,
        )
        acc.usage("diagnostician", result)
        data = parse_json_text(result.text)
    except Exception as e:  # noqa: BLE001
        acc.error(f"diagnostician: {e!r}")
        data = []

    valid_ph = set(catalog_by_ph(catalog))
    fn_names = set(srcmap)
    hyps = []
    seen_ph: set[str] = set()
    for i, d in enumerate(data if isinstance(data, list) else []):
        ph = _norm_ph(d.get("phenomenon_id", ""))
        fn = _norm_fn(d.get("suspect_function", ""), fn_names)
        if ph not in valid_ph or (fn and fn not in srcmap):
            acc.error(f"diagnostician: 丢弃假设[{i}] ph={d.get('phenomenon_id')!r} fn={d.get('suspect_function')!r}")
            continue
        if ph in fixed_prior:
            acc.error(f"diagnostician: 丢弃假设[{i}] {ph} 已修复（勿重提）")
            continue
        if not ph or ph in seen_ph:
            continue
        seen_ph.add(ph)
        hyps.append({
            "hypothesis_id": f"H{len(hyps) + 1}",
            "phenomenon_id": ph,
            "suspect_function": fn,
            "confidence": float(d.get("confidence", 0.5)),
            "rationale": str(d.get("rationale", "")),
            "evidence": [str(x) for x in d.get("evidence", [])],
            "source": "llm",
        })
    if not hyps:
        hyps = [h for h in hypotheses_from_probes(report) if h["phenomenon_id"] not in fixed_prior]
        acc.error("diagnostician: LLM 假设不可用，走纯代码兜底")
    first = hyps[0] if hyps else None
    return {**acc.out(), "hypotheses": hyps, "hypothesis_cursor": 0 if first else -1,
            "current_hypothesis": first, "patch_attempts": 0}
