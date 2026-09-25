r"""diagnostician — LLM 节点：三路证据 → 假设清单（自然语言问题）。

agent 不知道现象目录/bug 清单：假设的 problem 完全由探针告警证据、玩家
症状、视觉发现归纳而来（phenomenon_id 由 tester 的测试归因反推，框架内部
key）。首次进入生成（或兜底生成）假设清单；再次进入取下一个假设。
兜底 hypotheses_from_probes 为纯 Python：探针 signal → 问题假设（evidence
本身即现象级描述），suspect_function 留空由 patcher 全函数扫描。
"""
from __future__ import annotations

import json

from agent.nodes.common import get_llm_for, TokenDelta
from agent.tools.llm import llm_available, parse_json_text

SYSTEM = (
    "你是俄罗斯方块游戏的诊断专家。根据三路证据（遥测探针告警、玩家症状、视觉发现）"
    "归纳游戏存在的具体问题，提出修复假设清单。\n"
    "约束：\n"
    "1. problem 用一句话描述玩家可感知的具体问题（从证据归纳，不要臆造证据之外的想象）；\n"
    "2. suspect_function 必须取自候选函数列表（若证据不足以定位函数可留空字符串）；\n"
    "3. 每个问题只提一个假设，按置信度降序；\n"
    '4. 输出 JSON 数组：[{"problem": "问题描述", "suspect_function": "函数名",'
    ' "confidence": 0-1, "rationale": "理由", "evidence": ["证据要点"]}]\n'
    "5. already_fixed 中的问题已修复，勿再提出；previously_rejected 是此前局尝试"
    "修复未通过验证的问题，除非本局有新的明确证据，否则不要重复提出。\n"
    "只依据给定材料归纳，不要编造未出现的现象。"
)


def hypotheses_from_probes(report: dict) -> list[dict]:
    """兜底：探针 signal → 问题假设（evidence 即现象级描述）。"""
    out = []
    for probe_key, pr in report.get("probes", {}).items():
        if pr["status"] != "signal":
            continue
        ev = [str(x) for x in pr.get("evidence", [])]
        out.append({
            "hypothesis_id": f"H{len(out) + 1}",
            # evidence 首条即现象级描述；取前两条拼成问题
            "problem": "探针告警：" + "；".join(ev[:2]),
            "suspect_function": "",       # 由 patcher 兜底补齐（全函数扫描）
            "confidence": float(pr.get("confidence", 0.5)),
            "rationale": "探针信号直接映射（纯代码兜底）",
            "evidence": ev,
            "source": "fallback",
        })
    out.sort(key=lambda h: -h["confidence"])
    return out


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

    # 再次进入：推进到下一个假设（跳过已修复/已否决的同一问题文本）
    if hypotheses:
        done = ({str(f.get("hypothesis", "")).strip() for f in state.get("fixed_phenomena", [])}
                | {str(r.get("problem", "")).strip() for r in state.get("rejected", [])}) - {""}
        nxt = cursor + 1
        while nxt < len(hypotheses) and str(hypotheses[nxt].get("problem", "")).strip() in done:
            nxt += 1
        if nxt >= len(hypotheses) or nxt >= state.get("max_hypotheses", 5):
            return {**acc.out(), "current_hypothesis": None, "hypothesis_cursor": nxt}
        return {
            **acc.out(),
            "hypothesis_cursor": nxt,
            "current_hypothesis": hypotheses[nxt],
            "patch_attempts": 0,
        }

    # 首次进入：从三路证据生成假设
    report = state["probe_report"]
    mode = state.get("mode", "mock")
    srcmap = state.get("srcmap") or {}
    symptoms = state.get("feedback_symptoms") or []
    vision = state.get("vision_findings") or []
    fixed_prior = list(state.get("fixed_prior") or [])
    rejected_prior = list(state.get("rejected_prior") or [])

    if mode == "mock" or not llm_available("text"):
        hyps = hypotheses_from_probes(report)
        first = hyps[0] if hyps else None
        return {**acc.out(), "hypotheses": hyps, "hypothesis_cursor": 0 if first else -1,
                "current_hypothesis": first, "patch_attempts": 0}

    llm = get_llm_for(mode, "text")
    signal_evidence = [
        str(e) for v in report.get("probes", {}).values() if v["status"] == "signal"
        for e in v.get("evidence", [])
    ]
    user = {
        "probe_signals": signal_evidence or "（无）",
        "vision_findings": vision or "（无截图或未启用视觉）",
        "player_symptoms": symptoms or "（无）",
        "candidate_functions": sorted(srcmap.keys()),
        "already_fixed": fixed_prior or "（无）",
        "previously_rejected": rejected_prior or "（无）",
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

    fn_names = set(srcmap)
    hyps: list[dict] = []
    seen_problem: set[str] = set()
    for i, d in enumerate(data if isinstance(data, list) else []):
        if not isinstance(d, dict):
            continue
        problem = str(d.get("problem", "")).strip()
        fn = _norm_fn(d.get("suspect_function", ""), fn_names)
        if not problem:
            acc.error(f"diagnostician: 丢弃假设[{i}] problem 为空")
            continue
        if fn and fn not in srcmap:
            acc.error(f"diagnostician: 丢弃假设[{i}] fn={d.get('suspect_function')!r} 不在源码图")
            continue
        if problem in seen_problem or problem in fixed_prior:
            acc.error(f"diagnostician: 丢弃假设[{i}] {problem[:40]!r}（重复或已修复）")
            continue
        seen_problem.add(problem)
        hyps.append({
            "hypothesis_id": f"H{len(hyps) + 1}",
            "problem": problem,
            "suspect_function": fn,
            "confidence": float(d.get("confidence", 0.5)),
            "rationale": str(d.get("rationale", "")),
            "evidence": [str(x) for x in d.get("evidence", [])],
            "source": "llm",
        })
    if not hyps:
        hyps = hypotheses_from_probes(report)
        acc.error("diagnostician: LLM 假设不可用，走纯代码兜底")
    first = hyps[0] if hyps else None
    return {**acc.out(), "hypotheses": hyps, "hypothesis_cursor": 0 if first else -1,
            "current_hypothesis": first, "patch_attempts": 0}
