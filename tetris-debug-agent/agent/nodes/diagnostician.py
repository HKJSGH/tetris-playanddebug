r"""diagnostician — LLM 节点：三路证据 → 假设清单（自然语言问题）。

agent 不知道现象目录/bug 清单：假设的 problem 完全由探针告警证据、玩家
症状、视觉发现归纳而来（phenomenon_id 由 tester 的测试归因反推，框架内部
key）。诊断师配有三个只读查表工具（见 agent/tools/diag_tools.py）：
read_functions 读候选函数源码、query_probe_detail 查探针明细、query_events
查原始事件流——函数归因必须基于读过的源码，不得按函数名猜；怀疑探针
误报时用原始事件流核查。首次进入生成（或兜底生成）假设清单；再次进入
推进到下一个假设；当前假设清单已检验完（全部假设处理完或本批上限用完）
且本局有被否决的实证时，携带「原始证据 + 修复失败的实证」重诊断一轮
（轮数上限 MAX_REDIAG_ROUNDS），产出下一批假设——补上 tester 侧测试证据
的回流闭环。
兜底 hypotheses_from_probes 为纯 Python：探针 signal → 问题假设（evidence
本身即现象级描述），suspect_function 留空由 patcher 全函数扫描。

失败语义：mock/无 key 走兜底是合法显式降级；live 模式下 llm.chat 的任何
失败（LLMError）不捕获 → 冒泡中止整局（静默兜底=用假假设冒充真分析，
会污染实验数据）。解析失败=模型输出质量问题，仍走兜底（正常失败路径）。
"""
from __future__ import annotations

import json

from agent.config import MAX_HYPOTHESES, MAX_REDIAG_ROUNDS, MAX_TOOL_ROUNDS
from agent.nodes.common import get_llm_for, ph_to_b, TokenDelta
from agent.tools.diag_tools import build_diag_tools, function_digest
from agent.tools.llm import llm_available, parse_json_text

SYSTEM = (
    "你是俄罗斯方块游戏的诊断专家。根据三路证据（遥测探针告警、玩家症状、视觉发现）"
    "归纳游戏存在的具体问题，提出修复假设清单。\n"
    "你可以调用三个只读工具收集证据后再归因：\n"
    "- read_functions：读取候选函数的完整源码——给出 suspect_function 前必须读过"
    "该函数的源码，没读过的函数不得作为嫌疑函数（留空字符串）；\n"
    "- query_probe_detail：查看某个探针告警的完整统计明细；\n"
    "- query_events：查询本局原始事件流——怀疑探针告警可能是误报时，用它核查原始事件。\n"
    "约束：\n"
    "1. problem 用一句话描述玩家可感知的具体问题（从证据归纳，不要臆造证据之外的想象）；\n"
    "2. suspect_function 必须取自候选函数列表，且只填你用工具读过源码的函数"
    "（若证据不足以定位函数可留空字符串）；\n"
    "3. 每个问题只提一个假设，按置信度降序；\n"
    '4. 证据收集完成后，输出最终 JSON 数组：[{"problem": "问题描述",'
    ' "suspect_function": "函数名", "confidence": 0-1, "rationale": "理由",'
    ' "evidence": ["证据要点"]}]，不要输出多余解释；\n'
    "5. already_fixed 中的问题已修复，勿再提出；previously_rejected 是此前局尝试"
    "修复未通过验证的问题（附改法教训，即此前失败的方向），除非本局有新的明确证据，"
    "否则不要重复提出相同问题或相同改法方向。\n"
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
            # 框架内部弱归因（评估账本用；不进任何 prompt）
            "probe_key": probe_key,
        })
    out.sort(key=lambda h: -h["confidence"])
    return out


def _drop_fixed(hyps: list[dict], fixed_prior_ph: list[str]) -> list[dict]:
    """兜底假设同样遵守「已修复勿提」：探针键落在跨局已修复基线内的直接剔除。

    fixed_prior_ph 为框架内部账本（ingest 从 fixes.json 提取的 PH 集合），
    纯代码过滤用，不进任何 prompt。
    """
    fixed_b = {ph_to_b(str(ph)) for ph in fixed_prior_ph if ph}
    return [h for h in hyps
            if not h.get("probe_key") or h["probe_key"] not in fixed_b]


def _match_probe_key(evidence: list[str], report: dict) -> str:
    """纯代码弱归因：假设证据与探针证据文本互为子串 → 探针键。

    仅用于评估账本（probe_key 字段），文本留在纯代码层比对，
    键名不进任何 LLM prompt。匹配不到返回空串（诚实留空）。
    """
    items = [str(e) for e in evidence if str(e).strip()]
    for key, pr in report.get("probes", {}).items():
        if pr.get("status") != "signal":
            continue
        for pe in (str(x) for x in pr.get("evidence", [])):
            if not pe.strip():
                continue
            if any(pe in it or it in pe for it in items):
                return key
    return ""


def _norm_fn(raw: str, names: set[str]) -> str:
    """容错归一化函数名：去括号尾部、剥类前缀（TetrisGame._on_right → _on_right）。"""
    fn = str(raw).strip()
    if "(" in fn:
        fn = fn.split("(", 1)[0].strip()
    if fn not in names and "." in fn and fn.rsplit(".", 1)[-1] in names:
        fn = fn.rsplit(".", 1)[-1]
    return fn


def _user_payload(state: dict, extra: dict | None = None,
                  probe_labels: dict | None = None) -> dict:
    """诊断输入（键序即缓存前缀：跨局稳定段在前、逐局变化段居后、附加段最后）。

    candidate_functions 是最大且跨局不变的前缀段——形态为「签名摘要表」
    （函数名 — def 签名 — docstring 首行，见 diag_tools.function_digest），
    让诊断师有依据地挑选 read_functions 的阅读目标，而不是只按名字猜；
    already_fixed / previously_rejected 跨局缓增；三路证据逐局变化；
    extra（重诊断实证）最易变，永远垫底——保证重诊断多次调用时前缀尽量
    逐字稳定。

    probe_signals 只含 signal 探针（probe_labels 为 diag_tools 分配的每局
    临时标签 P1/P2/...；内部键绝不出现）。列全量探针索引会让 LLM 数出探针
    总数、暗示 bug 数量，属泄漏，禁止。
    """
    report = state["probe_report"]
    srcmap = state.get("srcmap") or {}
    if probe_labels is None:
        # 无标签时的旧形态：纯证据文本（仅兜底用，正常路径都带标签）
        signal_evidence = [
            str(e) for v in report.get("probes", {}).values() if v["status"] == "signal"
            for e in v.get("evidence", [])
        ]
        probe_signals = signal_evidence or "（无）"
    else:
        probe_signals = [
            {"label": probe_labels[key],
             "evidence": [str(x) for x in v.get("evidence", [])]}
            for key, v in sorted((report.get("probes") or {}).items())
            if v.get("status") == "signal" and key in probe_labels
        ] or "（无）"
    symptoms = [s for s in (state.get("feedback_symptoms") or [])
                if s.get("kind") != "suggestion"]
    user = {
        "candidate_functions": function_digest(srcmap),
        "already_fixed": list(state.get("fixed_prior") or []) or "（无）",
        "previously_rejected": list(state.get("rejected_prior") or []) or "（无）",
        "probe_signals": probe_signals,
        "vision_findings": (list(state.get("vision_findings") or [])
                            or "（无截图或未启用视觉）"),
        "player_symptoms": symptoms or "（无）",
    }
    if extra:
        user.update(extra)
    return user


def _diagnose_with_source(state: dict, payload: dict, acc: TokenDelta,
                          tools_schema: list[dict], execute) -> object:
    """带源码阅读工具的诊断循环（function calling），返回解析后的最终 JSON。

    流程：messages = [system, user(payload)]；循环 ≤ MAX_TOOL_ROUNDS 轮——
    模型发起工具调用则逐个执行并以 tool 消息回填继续；模型不再调工具则
    把该轮 text 作为最终答案解析返回。轮数用完模型仍在调工具时，追加
    「额度已用完」的 user 消息并去掉 tools 强制作答一次。

    失败语义：llm.chat 的一切失败（LLMError）不捕获 → 冒泡中止整局；
    parse_json_text 的解析失败（JSONDecodeError）也不在这里捕获，由调用方
    按现有语义处理（首次诊断→纯代码兜底假设；重诊断→记 error 收场）。
    每轮调用都 acc.usage 记账，token 分账按轮累计。
    """
    llm = get_llm_for(state.get("mode", "mock"), "text")
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    for _ in range(MAX_TOOL_ROUNDS):
        result = llm.chat(messages, tools=tools_schema, temperature=0.2)
        acc.usage("diagnostician", result)
        if not result.tool_calls:
            return parse_json_text(result.text)
        # assistant 消息须原样回填 tool_calls（OpenAI 协议要求 id/name/arguments）
        messages.append({
            "role": "assistant",
            "content": result.text or None,
            "tool_calls": [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in result.tool_calls
            ],
        })
        for tc in result.tool_calls:
            out = execute(tc["name"], tc["arguments"])
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
    # 工具轮数用完：强制作答收尾（不带 tools，模型无法再发起调用）
    messages.append({"role": "user",
                     "content": "工具调用额度已用完，请基于已获得的信息输出最终假设清单 JSON 数组。"})
    final = llm.chat(messages, temperature=0.2)
    acc.usage("diagnostician", final)
    return parse_json_text(final.text)


def _validated_hyps(data, srcmap: dict, report: dict, fixed_prior: list[str],
                    banned_problems: set[str], acc: TokenDelta) -> list[dict]:
    """LLM 输出 → 合法假设清单（归一化校验 + 去重 + 已修复/已否决剔除）。"""
    fn_names = set(srcmap)
    hyps: list[dict] = []
    seen = set(banned_problems)
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
        if problem in seen:
            acc.error(f"diagnostician: 丢弃假设[{i}] {problem[:40]!r}（重复/已修复/已否决）")
            continue
        seen.add(problem)
        hyp_evidence = [str(x) for x in d.get("evidence", [])]
        hyps.append({
            "hypothesis_id": f"H{len(hyps) + 1}",
            "problem": problem,
            "suspect_function": fn,
            "confidence": float(d.get("confidence", 0.5)),
            "rationale": str(d.get("rationale", "")),
            "evidence": hyp_evidence,
            "source": "llm",
            # 纯代码回填的内部弱归因（评估账本用；不进任何 prompt）
            "probe_key": _match_probe_key(hyp_evidence, report),
        })
    return hyps


def _rediagnose_or_finish(state: dict, cursor_pos: int, acc: TokenDelta) -> dict:
    """当前假设清单已检验完：本局有失败实证且轮数未用完 → 重诊断一轮，否则收场。

    失败语义与首次诊断一致：live 下 llm.chat 失败（LLMError）冒泡中止整局；
    解析失败/未产出可用新假设 → 记 errors 后收场（rediag_rounds 仍 +1，防循环）。
    """
    finished = {**acc.out(), "hypothesis_cursor": cursor_pos, "current_hypothesis": None}
    mode = state.get("mode", "mock")
    rediag_rounds = state.get("rediag_rounds", 0)
    if (mode == "mock" or not llm_available("text")
            or rediag_rounds >= MAX_REDIAG_ROUNDS):
        return finished
    rejected = state.get("rejected") or []
    attempt_log = state.get("attempt_log") or []
    if not rejected and not any(not a.get("ok") for a in attempt_log):
        return finished     # 无失败实证（假设全部修复），无需重诊断

    # 重诊断实证段（易变信息，永远在 payload 末尾，见 _user_payload 键序说明）
    failures_by_problem: dict[str, list[str]] = {}
    for a in attempt_log:
        p = str(a.get("problem", "")).strip()
        err = str(a.get("error", "") or "").strip()
        if p and not a.get("ok") and err:
            errs = failures_by_problem.setdefault(p, [])
            if err[:120] not in errs:
                errs.append(err[:120])
    extra = {
        "本局已修复问题": [str(f.get("hypothesis", "")).strip()
                       for f in state.get("fixed_phenomena") or []] or "（无）",
        "本局已否决假设": [
            {"problem": str(r.get("problem", "")),
             "attempts": r.get("attempts", 0),
             "改法教训": str(r.get("lesson", "") or ""),
             "最后失败": str(r.get("last_error", ""))[:120]}
            for r in rejected],
        "本局失败原因汇总": failures_by_problem or "（无）",
        "重诊断指令": (
            f"以上假设清单已检验完（已修复 {len(state.get('fixed_phenomena') or [])} 项、"
            f"已否决 {len(rejected)} 项）。请基于原始证据与上述修复失败的实证，"
            "重新归纳尚未尝试的问题假设：不要重复已修复或已否决的问题；"
            "也不要提出与改法教训相同方向的假设。"
            "若证据不足以提出新假设，输出空数组 []。"
        ),
    }
    tools_schema, execute, probe_labels = build_diag_tools(state)
    try:
        data = _diagnose_with_source(
            state, _user_payload(state, extra, probe_labels), acc, tools_schema, execute)
    except json.JSONDecodeError as e:  # noqa: BLE001 — 解析失败=输出质量问题
        acc.error(f"diagnostician: 重诊断解析失败 {e!r}")
        data = []

    banned = (set(state.get("fixed_prior") or [])
              | {str(f.get("hypothesis", "")).strip() for f in state.get("fixed_phenomena", [])}
              | {str(r.get("problem", "")).strip() for r in rejected}) - {""}
    new_hyps = _validated_hyps(data, state.get("srcmap") or {}, state["probe_report"],
                               state.get("fixed_prior") or [], banned, acc)[:MAX_HYPOTHESES]
    if not new_hyps:
        acc.error("diagnostician: 重诊断未产出可用新假设，收场")
        return {**finished, "rediag_rounds": rediag_rounds + 1}
    old = list(state.get("hypotheses") or [])
    return {
        **acc.out(),
        "hypotheses": old + new_hyps,
        "hypothesis_cursor": len(old),
        "hypothesis_batch_start": len(old),     # 新批次从旧清单末尾开始
        "current_hypothesis": new_hyps[0],
        "patch_attempts": 0,
        "rediag_rounds": rediag_rounds + 1,
    }


def node_diagnostician(state: dict) -> dict:
    acc = TokenDelta()
    acc.step("diagnostician")

    cursor = state.get("hypothesis_cursor", -1)
    hypotheses = state.get("hypotheses") or []

    # 再次进入：推进到下一个假设（跳过已修复/已否决的同一问题文本）
    if hypotheses:
        done = ({str(f.get("hypothesis", "")).strip() for f in state.get("fixed_phenomena", [])}
                | {str(r.get("problem", "")).strip() for r in state.get("rejected", [])}) - {""}
        batch_start = state.get("hypothesis_batch_start", 0)
        nxt = cursor + 1
        while nxt < len(hypotheses) and str(hypotheses[nxt].get("problem", "")).strip() in done:
            nxt += 1
        if nxt < len(hypotheses) and (nxt - batch_start) < state.get("max_hypotheses", MAX_HYPOTHESES):
            return {
                **acc.out(),
                "hypothesis_cursor": nxt,
                "current_hypothesis": hypotheses[nxt],
                "patch_attempts": 0,
            }
        # 当前假设清单已检验完（全部假设处理完或本批上限用完）→ 重诊断或收场
        return _rediagnose_or_finish(state, nxt, acc)

    # 首次进入：从三路证据生成假设
    mode = state.get("mode", "mock")
    srcmap = state.get("srcmap") or {}
    fixed_prior = list(state.get("fixed_prior") or [])
    fixed_prior_ph = list(state.get("fixed_prior_ph") or [])

    if mode == "mock" or not llm_available("text"):
        hyps = _drop_fixed(hypotheses_from_probes(state["probe_report"]), fixed_prior_ph)
        first = hyps[0] if hyps else None
        return {**acc.out(), "hypotheses": hyps, "hypothesis_cursor": 0 if first else -1,
                "current_hypothesis": first, "patch_attempts": 0}

    # llm.chat 失败不捕获：live 下 LLMError 冒泡中止整局（fail-fast，不静默兜底）
    tools_schema, execute, probe_labels = build_diag_tools(state)
    try:
        data = _diagnose_with_source(
            state, _user_payload(state, probe_labels=probe_labels), acc, tools_schema, execute)
    except json.JSONDecodeError as e:  # noqa: BLE001 — 解析失败=输出质量问题，走纯代码兜底
        acc.error(f"diagnostician: 解析失败 {e!r}")
        data = []

    hyps = _validated_hyps(data, srcmap, state["probe_report"],
                           fixed_prior, set(fixed_prior), acc)
    if not hyps:
        hyps = _drop_fixed(hypotheses_from_probes(state["probe_report"]), fixed_prior_ph)
        acc.error("diagnostician: LLM 假设不可用，走纯代码兜底")
    first = hyps[0] if hyps else None
    return {**acc.out(), "hypotheses": hyps, "hypothesis_cursor": 0 if first else -1,
            "current_hypothesis": first, "patch_attempts": 0}