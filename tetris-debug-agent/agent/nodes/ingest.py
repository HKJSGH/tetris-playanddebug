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
    fixed_prior_ph: list[str] = []
    if FIXES_PATH.exists():
        fx = json.loads(FIXES_PATH.read_text(encoding="utf-8"))
        # 跨局记忆只给自然语言问题文本（PH 编号是框架内部 key，不进 prompt）；
        # fixed_prior_ph 仅供纯代码过滤兜底假设（已修复勿提），同样不进 prompt
        fixed_prior = sorted({str(f.get("hypothesis", "")).strip()
                              for f in fx.get("fixed_phenomena", [])} - {""})
        # rejected_prior 增强：问题文本 + 失败模式摘要（尝试次数/嫌疑函数/最后失败），
        # 让诊断重提前"带着教训"——知道该问题试过修不动，考虑换函数或换角度
        rejected_prior = []
        for r in fx.get("rejected", []):
            p = str(r.get("problem", "")).strip()
            if not p:
                continue
            fn = str(r.get("suspect_function", "") or "").strip()
            err = str(r.get("last_error", "") or "").strip()
            s = f"「{p}」此前尝试 {r.get('attempts', '?')} 次未通过"
            if fn:
                s += f"（嫌疑函数 {fn}）"
            if err:
                s += f"，最后失败: {err[:80]}"
            rejected_prior.append(s)
        fixed_prior_ph = sorted({str(f.get("phenomenon_id", "")).strip()
                                 for f in fx.get("fixed_phenomena", [])} - {""})
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
        "fixed_prior_ph": fixed_prior_ph,
        **acc.out(),
        "hypotheses": [],
        "hypothesis_cursor": -1,
        "fixed_phenomena": [],
        "rejected": [],
        "deferred": [],
        "attempt_log": [],      # append-reducer 首值（tester 每尝试追加）
        "patch_error": "",
        "errors": [],
    }
