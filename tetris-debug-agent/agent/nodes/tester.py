r"""tester — 纯代码节点：应用补丁 → 全量受控测试归因 → 保留/回滚 → 路由。

归因制（agent 不知现象目录/bug 清单）：应用补丁后跑全部 tests/test_bugs
（test_B01..test_B12），与修复前基线对比——未修复 bug 的测试**整文件用例
全绿** ⇒ 归因修复了该 bug（Bxx 经纯代码转 PH-xx，进 fixes.json/eval）。

基线来源两处，保持一致：
  - 跨局：data/fixes.json 的 fixed_phenomena（历史局确认修复）——其测试
    文件要求全绿且计入「已修复基线」，再红即回归；
  - 本局：黑板 fixed_phenomena（本次 debug 会话已归因的）。
agent 侧口径（attributed 基线）与 golden 门控（GOLDEN_ALLOWED_PH ∪
fixes.json）必须用同一集合，否则会出现「test 绿但 golden 因门控未放行而
红」的假回归误杀。

golden 失败时把场景名与差异摘要回传 patcher（预期行为快照 vs 实际的
关键字段），LLM 才知道「差在哪」而不是只看到 failed 计数。
fail 从备份回滚；红线：agent 自写测试不参与。
"""
from __future__ import annotations

import json
import re

from agent.config import FIXES_PATH, MAX_PATCH_ATTEMPTS, TESTS_DIR
from agent.nodes.common import b_to_ph, ph_to_b, TokenDelta
from agent.tools.patch import apply_patch, revert_backup, run_pytest

ALL_BUGS = [f"B{i:02d}" for i in range(1, 13)]


def _fixed_ph_from_fixes() -> set[str]:
    """跨局已修复 PH（框架账本，纯代码读取；缺失/损坏视为空）。"""
    try:
        data = json.loads(FIXES_PATH.read_text(encoding="utf-8"))
        return {f["phenomenon_id"] for f in data.get("fixed_phenomena", [])}
    except Exception:  # noqa: BLE001
        return set()


def _broken_bugs(output: str) -> dict[str, set[str]]:
    """pytest 输出 → {Bxx: 失败用例名集合}（文件内有任一用例红即 broken）。"""
    broken: dict[str, set[str]] = {}
    for line in output.splitlines():
        m = re.search(r"(?:FAILED|ERROR)\s+\S*?test_(B\d+)\.py::(\S+)", line)
        if m:
            broken.setdefault(f"test_{m.group(1)}", set()).add(m.group(2))
    return broken


def _golden_fail_summary(golden_output: str) -> str:
    """golden 失败输出 → 逐场景差异摘要（回传 patcher 的「差在哪」）。"""
    scenes = re.findall(r"FAILED \S*?\[(SC_B\d+)\]", golden_output)
    lines = []
    for sc in scenes:
        lines.append(f"场景 {sc} 与干净版行为快照不一致")
    # 截取第一个失败断言附近的 E 行作为差异详情（pytest -q 的 assert 片段）
    e_lines = [l.strip() for l in golden_output.splitlines()
               if l.strip().startswith("E ")][:6]
    detail = "；".join(lines[:4]) or "golden 回归失败（无场景明细）"
    if e_lines:
        detail += "｜差异要点: " + " / ".join(x[:110] for x in e_lines[:3])
    return detail


def node_tester(state: dict) -> dict:
    acc = TokenDelta()
    acc.step("tester")
    hyp = dict(state.get("current_hypothesis") or {})
    patch = state.get("patch") or {"blocks": []}
    attempts = state.get("patch_attempts", 0) + 1
    fixed = list(state.get("fixed_phenomena") or [])
    max_att = state.get("max_patch_attempts", MAX_PATCH_ATTEMPTS)
    problem = str(hyp.get("problem", "")).strip()

    result = {"patch_attempts": attempts, **acc.out()}

    blocks = patch.get("blocks") or []
    if not blocks:
        hyp["attempts"] = attempts
        hyp["last_error"] = "patcher 未产出有效补丁"
        result["current_hypothesis"] = hyp
        result["test_result"] = {"ok": False, "stage": "patch", "error": hyp["last_error"]}
    elif not problem:
        hyp["attempts"] = attempts
        hyp["last_error"] = "无有效假设"
        result["current_hypothesis"] = hyp
        result["test_result"] = {"ok": False, "stage": "hypothesis", "error": hyp["last_error"]}
    else:
        applied = apply_patch(blocks)
        if not applied.ok:
            hyp["attempts"] = attempts
            hyp["last_error"] = applied.error
            result["current_hypothesis"] = hyp
            result["test_result"] = {"ok": False, "stage": "apply", "error": applied.error}
        else:
            golden_out = ""  # golden 只在归因成功后运行；失败路径统一取尾部输出
            # ---- 归因基线：跨局（fixes.json）∪ 本局（黑板），两处同源 ----
            base_ph = {f["phenomenon_id"] for f in fixed} | _fixed_ph_from_fixes()
            fixed_b = {ph_to_b(ph) for ph in base_ph}

            # 段 1：全量 test_bugs → 与基线对比归因
            tr = run_pytest([TESTS_DIR / "test_bugs"])
            broken = _broken_bugs(tr.output)
            broken_b = {b.removeprefix("test_") for b in broken}
            regressions = sorted(fixed_b & broken_b)          # 基线内变红 = 修坏旧账
            # 归因只认「基线外整文件全绿」（文件内任一用例红都不算）
            newly = [b for b in ALL_BUGS if b not in broken_b and b not in fixed_b]
            no_tests = "no tests ran" in tr.output

            if not no_tests and newly and not regressions:
                # 段 2：golden 等价回归（门控 = 基线 ∪ 本轮归因，与 agent 侧同源）
                allowed = sorted(base_ph | {b_to_ph(b) for b in newly})
                tr_golden = run_pytest([TESTS_DIR / "golden"], allowed_ph=allowed)
                golden_out = tr_golden.output
                if tr_golden.failed == 0:
                    result["test_result"] = {
                        "ok": True, "stage": "test",
                        "passed": tr.passed, "failed": tr.failed,
                        "skipped": tr.skipped + tr_golden.skipped,
                        "attributed": newly,
                        "output_tail": tr.output[-800:],
                    }
                    hyp["attempts"] = attempts
                    for b in newly:
                        fixed.append({
                            "phenomenon_id": b_to_ph(b),
                            "suspect_function": hyp.get("suspect_function", ""),
                            "hypothesis": problem,
                            "patch": {"blocks": blocks, "note": patch.get("note", "")},
                            "tests_passed": {"passed": tr.passed, "skipped": tr.skipped},
                            "attempts_used": attempts,
                        })
                    result.update({"fixed_phenomena": fixed, "current_hypothesis": hyp})
                    return result
                hyp["last_error"] = ("golden 等价回归失败，已回滚："
                                     + _golden_fail_summary(tr_golden.output))
            elif regressions:
                hyp["last_error"] = f"已修复测试回归: {','.join(regressions)}，已回滚"
            elif no_tests:
                hyp["last_error"] = "pytest 未收集到任何测试（环境异常），已回滚"
            else:
                still = sorted(broken_b - fixed_b) or ["（无失败明细）"]
                hyp["last_error"] = f"补丁未使任何受控测试转绿（仍失败: {','.join(still[:6])}）"
            result["test_result"] = {
                "ok": False, "stage": "test",
                "passed": tr.passed, "failed": tr.failed, "skipped": tr.skipped,
                "error": hyp["last_error"],
                "output_tail": (tr.output + golden_out)[-2500:],
            }
            revert_backup(applied.backup_path)

    # 失败：未耗尽 → 回 patcher；耗尽 → 在节点内记 rejected（路由函数无法写 state）
    if attempts < max_att:
        return result
    rejected = list(state.get("rejected") or [])
    if problem and problem not in {r.get("problem") for r in rejected}:
        rejected.append({
            "problem": problem,
            "suspect_function": hyp.get("suspect_function", ""),
            "attempts": attempts,
            "last_error": hyp.get("last_error", ""),
        })
        result["rejected"] = rejected
    return result


def route_after_tester(state: dict) -> str:
    tr = state.get("test_result") or {}
    if tr.get("ok"):
        return "wrapup"
    hyp = state.get("current_hypothesis") or {}
    max_att = state.get("max_patch_attempts", MAX_PATCH_ATTEMPTS)
    rejected_problems = {r.get("problem") for r in state.get("rejected", [])}
    problem = str(hyp.get("problem", "")).strip()
    if state.get("patch_attempts", 0) < max_att and problem and problem not in rejected_problems:
        return "patcher"
    # 当前假设已耗尽（记入 rejected）或无假设：找下一个可用假设
    done = ({str(f.get("hypothesis", "")).strip() for f in state.get("fixed_phenomena", [])}
            | rejected_problems) - {""}
    cursor = state.get("hypothesis_cursor", -1)
    hypotheses = state.get("hypotheses") or []
    nxt = cursor + 1
    while nxt < len(hypotheses) and str(hypotheses[nxt].get("problem", "")).strip() in done:
        nxt += 1
    if nxt < len(hypotheses) and nxt < state.get("max_hypotheses", 5):
        return "diagnostician"
    return "wrapup"
