"""nodes 公共层：LLM 获取、B↔PH 编号转换、记账工具。

B↔PH 转换仅纯代码使用（tester 测试文件定位、兜底映射、eval），
任何 LLM prompt 一律只出现 PH 编号与现象级描述。
"""
from __future__ import annotations

import functools

import yaml

from agent.config import CATALOG_PATH, TRUTH_MAP_PATH
from agent.tools.llm import MockLLM, get_llm


def get_llm_for(mode: str, role: str):
    """mode=mock 强制 MockLLM；否则有 key 用真模型，无 key 自动降级。"""
    if mode == "mock":
        return MockLLM(role)
    return get_llm(role)


@functools.lru_cache(maxsize=1)
def load_catalog() -> dict:
    return yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=1)
def load_truth_map() -> dict:
    import json

    return json.loads(TRUTH_MAP_PATH.read_text(encoding="utf-8"))


def b_to_ph(bug_id: str) -> str:
    """B01 → PH-01（探针内部编号转现象号；仅纯代码路径）。"""
    return f"PH-{int(bug_id[1:]):02d}"


def ph_to_b(ph_id: str) -> str:
    return f"B{int(ph_id[3:]):02d}"


def catalog_by_ph(catalog: dict | None = None) -> dict:
    catalog = catalog or load_catalog()
    return {p["id"]: p for p in catalog["phenomena"]}


class TokenDelta:
    """节点内记账累加器：节点结束时 out() 返回增量供 merge_tokens reducer 合并。"""

    def __init__(self):
        self.d = {
            "n_llm_calls": 0, "n_graph_steps": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "by_agent": {}, "errors": [],
        }

    def step(self, node: str) -> None:
        self.d["n_graph_steps"] += 1
        self.d["by_agent"].setdefault(node, {"n_calls": 0, "prompt_tokens": 0, "completion_tokens": 0})

    def usage(self, agent: str, result) -> None:
        p = getattr(result, "prompt_tokens", 0)
        c = getattr(result, "completion_tokens", 0)
        self.d["n_llm_calls"] += 1
        self.d["prompt_tokens"] += p
        self.d["completion_tokens"] += c
        agg = self.d["by_agent"].setdefault(agent, {"n_calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        agg["n_calls"] += 1
        agg["prompt_tokens"] += p
        agg["completion_tokens"] += c

    def error(self, msg: str) -> None:
        self.d["errors"].append(msg)

    def out(self) -> dict:
        return {"tokens": self.d}


def init_tokens() -> dict:
    """兼容保留：空 tokens 基值（reducer 模式下节点一般用 TokenDelta.out()）。"""
    return {
        "n_llm_calls": 0, "n_graph_steps": 0, "prompt_tokens": 0,
        "completion_tokens": 0, "by_agent": {}, "errors": [],
    }
