"""ingest — 纯代码节点：加载局数据 + 跑探针 + 载入 catalog + 解析源码图 + 读跨局记忆。"""
from __future__ import annotations

import json

from agent.config import FIXES_PATH
from agent.nodes.common import TokenDelta, load_catalog
from agent.tools.probes import load_round, run_all_probes
from agent.tools.srcmap import build_srcmap


def node_ingest(state: dict) -> dict:
    rd = load_round(state["run_dir"])
    report = run_all_probes(rd)
    catalog = load_catalog()
    sm = build_srcmap()
    fixed_prior: list[str] = []
    rejected_prior: list[str] = []
    if FIXES_PATH.exists():
        fx = json.loads(FIXES_PATH.read_text(encoding="utf-8"))
        fixed_prior = [f["phenomenon_id"] for f in fx.get("fixed_phenomena", [])]
        rejected_prior = sorted({r["phenomenon_id"] for r in fx.get("rejected", [])})
    acc = TokenDelta()
    acc.step("ingest")
    return {
        "round_id": rd.meta.get("round_id", state["round_id"]),
        "round_data": rd,
        "probe_report": report,
        "catalog": catalog,
        "srcmap": sm.functions,
        "constants_src": sm.constants,
        "fixed_prior": fixed_prior,
        "rejected_prior": rejected_prior,
        **acc.out(),
        "hypotheses": [],
        "hypothesis_cursor": -1,
        "fixed_phenomena": [],
        "rejected": [],
        "deferred": [],
        "errors": [],
    }
