r"""patcher — LLM 节点：单假设（自然语言问题）+ 嫌疑函数源码 → SEARCH/REPLACE 补丁。

上下文分层（prompt cache 友好，同假设重试间前缀逐字相同才命中）：
  [不变段]   问题/假设/嫌疑函数源码/常量/函数名 —— 同假设 N 次重试逐字相同
  [缓增段]   尝试历史（append-only：第 N 次 = 第 N-1 次内容 + 1 条摘要）
  [易变段]   上次失败信息 —— 永远放最后
历史条目只带结构化摘要（不进补丁全文——防膨胀、防对旧补丁锚定）；
补丁全文只落盘 patch_history.jsonl（wrapup），绝不回流 prompt。

兜底（Mock/无 key/解析失败）：patch 为空 → tester 判定该尝试失败。
live 模式下 llm.chat 的任何失败（LLMError）不捕获，冒泡中止整局。
无有效产出时把原因写进 state["patch_error"]，由 tester 记入尝试历史。
suspect_function 为空时给 LLM 全部函数源码让它自行定位。
"""
from __future__ import annotations

import json

from agent.nodes.common import get_llm_for, TokenDelta
from agent.tools.llm import llm_available, parse_json_text

SYSTEM = (
    "你是资深游戏修复工程师。针对一个诊断假设（玩家可感知的具体问题），"
    "给出最小 SEARCH/REPLACE 补丁。\n"
    "约束：\n"
    "1. search 必须是目标源码中原样存在的连续片段（保持缩进），且在文件中恰好出现一次；\n"
    "2. replace 是修复后的完整片段；改动最小化，只修该假设对应的问题；\n"
    '3. 输出 JSON：{"blocks": [{"search": "...", "replace": "..."}], "note": "修复说明"}\n'
    "问题（problem）是 QA 从证据归纳的现象描述，需结合嫌疑函数源码理解其指向的"
    "具体问题；只修该问题。\n"
    "若提供了「尝试历史」，说明之前的尝试未成功——不要重复相同思路："
    "search 应用失败（未命中/不唯一）就换更短的锚定片段；"
    "补丁应用了但测试未过就换修复位置或修复思路。"
    "不要输出多余解释。"
)


def _attempt_history(state: dict) -> list[dict]:
    """本假设的历次尝试摘要（缓增段；不含时间戳/补丁全文，保证前缀逐字追加）。"""
    problem = str((state.get("current_hypothesis") or {}).get("problem", "")).strip()
    out = []
    for a in state.get("attempt_log") or []:
        if a.get("problem") != problem:
            continue
        out.append({
            "attempt": a.get("attempt"),
            "applied": bool(a.get("applied")),
            "目标": a.get("suspect_function") or "（全函数扫描）",
            "结果": (str(a.get("error", ""))[:120] if not a.get("ok") else "已修复"),
        })
    return out


def node_patcher(state: dict) -> dict:
    acc = TokenDelta()
    acc.step("patcher")
    hyp = state.get("current_hypothesis") or {}
    mode = state.get("mode", "mock")
    srcmap = state.get("srcmap") or {}
    constants = state.get("constants_src") or {}

    empty = {"patch": {"blocks": [], "note": ""}, "patch_error": "", **acc.out()}

    if mode == "mock" or not llm_available("text"):
        return empty

    fn = hyp.get("suspect_function", "")
    if fn and fn in srcmap:
        fn_src = srcmap[fn]
    else:
        # 无嫌疑函数时必须给全部函数源码：只给函数名会让 LLM 产出
        # 无法应用的碎片补丁（search 在文件中多处出现或凭空捏造）
        fn_src = "（未定位嫌疑函数；以下为文件全部函数源码，请自行定位后给补丁）\n\n" + "\n\n".join(
            f"### {name}\n{body}" for name, body in sorted(srcmap.items())
        )

    llm = get_llm_for(mode, "text")
    # 键序即缓存前缀：不变段在前，缓增段居中，易变段永远最后（json.dumps 保持插入序）
    user = {
        # ---- 不变段（同假设重试逐字相同）----
        "问题": {
            "problem": hyp.get("problem", ""),
        },
        "假设": {
            "suspect_function": fn or "未定位",
            "rationale": hyp.get("rationale", ""),
            "evidence": hyp.get("evidence", []),
        },
        "嫌疑函数源码": fn_src,
        "文件顶部常量": constants,
        "全部函数名": sorted(srcmap.keys()),
        # ---- 缓增段（append-only 尝试历史）----
        "尝试历史": _attempt_history(state) or "（无，这是第 1 次尝试）",
        # ---- 易变段（本次新信息）----
        "上次尝试失败信息": hyp.get("last_error", "") or "（无）",
    }
    result = llm.chat(
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
        temperature=0.2,
    )
    acc.usage("patcher", result)
    try:
        data = parse_json_text(result.text)
        blocks = [
            {"search": str(b["search"]), "replace": str(b["replace"])}
            for b in data.get("blocks", []) if isinstance(b, dict)
        ] if isinstance(data, dict) else []
    except Exception as e:  # noqa: BLE001 — 解析失败=模型输出质量问题，属正常尝试失败
        acc.error(f"patcher: 解析失败 {e!r}")
        blocks, data = [], {}
    if not blocks:
        reason = "LLM 未产出有效 blocks"
        raw = getattr(result, "text", "") or ""
        patch_error = f"{reason}（raw[:160]={raw[:160]!r}）" if raw else reason
        acc.error(f"patcher: {reason}，raw[:180]={raw[:180]!r}")
        return {"patch": {"blocks": [], "note": ""}, "patch_error": patch_error, **acc.out()}
    return {"patch": {"blocks": blocks, "note": str(data.get("note", ""))},
            "patch_error": "", **acc.out()}
