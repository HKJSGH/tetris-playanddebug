"""PipelineState — 黑板模式共享状态（LangGraph StateGraph）。

各节点返回 partial state（只写自己关心的字段），graph 负责合并。
对象字段（round_data 等）仅进程内存传递，不落 checkpointer。
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict


def merge_tokens(a: dict | None, b: dict | None) -> dict:
    """tokens 并发合并 reducer：数值相加、by_agent 累加、errors 拼接。"""
    a, b = a or {}, b or {}
    keys = ("n_llm_calls", "n_graph_steps", "prompt_tokens", "completion_tokens")
    by: dict = {}
    for src in (a, b):
        for agent, v in src.get("by_agent", {}).items():
            tgt = by.setdefault(agent, {"n_calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
            for k in tgt:
                tgt[k] += v.get(k, 0)
    return {
        **{k: a.get(k, 0) + b.get(k, 0) for k in keys},
        "by_agent": by,
        "errors": a.get("errors", []) + b.get("errors", []),
    }


class Hypothesis(TypedDict, total=False):
    hypothesis_id: str
    phenomenon_id: str          # PH-xx
    suspect_function: str
    confidence: float
    rationale: str
    evidence: list[str]
    source: str                 # llm | fallback
    outcome: str                # fixed | rejected | deferred
    attempts: int
    last_error: str


class PipelineState(TypedDict, total=False):
    # 输入
    round_id: int
    run_dir: str
    mode: str                       # mock | llm
    max_patch_attempts: int
    max_hypotheses: int
    start_ts: float

    # ingest 产物
    round_data: Any                 # RoundData 对象
    probe_report: dict
    catalog: dict
    srcmap: dict                    # 函数名 → 源码段
    constants_src: dict             # 顶层常量名 → 源码段
    fixed_prior: list[str]          # 跨局记忆：fixes.json 已修复 PH
    rejected_prior: list[str]       # 跨局记忆：历史局已否决 PH（新证据可重试）

    # 证据节点产物
    vision_findings: list[dict]     # [{ticket_id, image, observations}]
    vision_skipped: bool
    feedback_symptoms: list[dict]   # [{ticket_id, text, source}]

    # 诊断 → 修复循环
    hypotheses: list[Hypothesis]
    hypothesis_cursor: int
    current_hypothesis: Hypothesis | None
    patch: dict                     # {"blocks": [{"search","replace"}], "note": str}
    patch_attempts: int             # 当前假设已用尝试次数
    test_result: dict

    # 结果
    fixed_phenomena: list[dict]     # 本局确认修复
    rejected: list[dict]
    deferred: list[dict]
    errors: list[str]

    # 记账（节点只写增量，merge_tokens reducer 累加）
    tokens: Annotated[dict, merge_tokens]
