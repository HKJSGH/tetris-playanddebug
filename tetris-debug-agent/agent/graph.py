"""graph — LangGraph StateGraph 组装。

拓扑：START→ingest→(vision ∥ feedback)→diagnostician→patcher→tester；
diagnostician 条件边：有 current_hypothesis→patcher｜无（clean 局/假设耗尽）→wrapup，
避免无假设时白走 patcher/tester；
tester 条件边：passed→wrapup｜fail<3→patcher（带失败日志）｜
fail≥3 且有下一假设→diagnostician（换假设）｜否则→wrapup。
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agent.nodes.diagnostician import node_diagnostician
from agent.nodes.feedback import node_feedback
from agent.nodes.ingest import node_ingest
from agent.nodes.patcher import node_patcher
from agent.nodes.tester import node_tester, route_after_tester
from agent.nodes.vision import node_vision
from agent.nodes.wrapup import node_wrapup
from agent.state import PipelineState


def route_after_diagnostician(state: dict) -> str:
    return "patcher" if state.get("current_hypothesis") else "wrapup"


def build_graph():
    g = StateGraph(PipelineState)
    g.add_node("ingest", node_ingest)
    g.add_node("vision", node_vision)
    g.add_node("feedback", node_feedback)
    g.add_node("diagnostician", node_diagnostician)
    g.add_node("patcher", node_patcher)
    g.add_node("tester", node_tester)
    g.add_node("wrapup", node_wrapup)

    g.add_edge(START, "ingest")
    g.add_edge("ingest", "vision")
    g.add_edge("ingest", "feedback")
    g.add_edge("vision", "diagnostician")
    g.add_edge("feedback", "diagnostician")
    g.add_conditional_edges(
        "diagnostician",
        route_after_diagnostician,
        {"patcher": "patcher", "wrapup": "wrapup"},
    )
    g.add_edge("patcher", "tester")
    g.add_conditional_edges(
        "tester",
        route_after_tester,
        {"patcher": "patcher", "diagnostician": "diagnostician", "wrapup": "wrapup"},
    )
    g.add_edge("wrapup", END)
    return g.compile()
