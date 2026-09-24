r"""patcher — LLM 节点：单假设 + 嫌疑函数源码 → SEARCH/REPLACE 补丁。

兜底（Mock/无 key/解析失败）：patch 为空 → tester 判定该尝试失败。
suspect_function 为空时给 LLM 全部函数名列表让它自行定位。
"""
from __future__ import annotations

import json

from agent.nodes.common import catalog_by_ph, get_llm_for, TokenDelta
from agent.tools.llm import llm_available, parse_json_text

SYSTEM = (
    "你是资深游戏修复工程师。针对一个诊断假设，给出最小 SEARCH/REPLACE 补丁。\n"
    "约束：\n"
    "1. search 必须是目标源码中原样存在的连续片段（保持缩进），且在文件中恰好出现一次；\n"
    "2. replace 是修复后的完整片段；改动最小化，只修该假设对应的问题；\n"
    '3. 输出 JSON：{"blocks": [{"search": "...", "replace": "..."}], "note": "修复说明"}\n'
    "现象线索（clue）是代码遗留备注，需结合嫌疑函数源码理解其指向的具体问题；只修该问题。"
    "不要输出多余解释。"
)


def node_patcher(state: dict) -> dict:
    acc = TokenDelta()
    acc.step("patcher")
    hyp = state.get("current_hypothesis") or {}
    mode = state.get("mode", "mock")
    srcmap = state.get("srcmap") or {}
    constants = state.get("constants_src") or {}

    empty = {"patch": {"blocks": [], "note": ""}, **acc.out()}

    if mode == "mock" or not llm_available("text"):
        return empty

    catalog = catalog_by_ph()
    ph = hyp.get("phenomenon_id", "")
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
    user = {
        "现象": {
            "id": ph,
            "clue": catalog.get(ph, {}).get("clue", ""),
            "observable_via": catalog.get(ph, {}).get("observable_via", []),
        },
        "假设": {
            "suspect_function": fn or "未定位",
            "rationale": hyp.get("rationale", ""),
            "evidence": hyp.get("evidence", []),
        },
        "上次尝试失败信息": hyp.get("last_error", "") or "（无）",
        "嫌疑函数源码": fn_src,
        "文件顶部常量": constants,
        "全部函数名": sorted(srcmap.keys()),
    }
    try:
        result = llm.chat(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
            temperature=0.2,
        )
        acc.usage("patcher", result)
        data = parse_json_text(result.text)
        blocks = [
            {"search": str(b["search"]), "replace": str(b["replace"])}
            for b in data.get("blocks", []) if isinstance(b, dict)
        ] if isinstance(data, dict) else []
        if not blocks:
            acc.error(f"patcher: LLM 未产出有效 blocks，raw[:180]={result.text[:180]!r}")
            return empty
        return {"patch": {"blocks": blocks, "note": str(data.get("note", ""))}, **acc.out()}
    except Exception as e:  # noqa: BLE001
        acc.error(f"patcher: {e!r}")
        return empty
