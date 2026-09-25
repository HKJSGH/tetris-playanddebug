r"""tester — 纯代码节点：应用补丁 → 全量受控测试归因 → 保留/回滚 → 路由。

归因制（agent 不知现象目录/bug 清单）：应用补丁后跑全部 tests/test_bugs
（test_B01..test_B12），与修复前基线对比——未修复 bug 的测试由红转绿
⇒ 归因修复了该 bug（Bxx 经纯代码转 PH-xx，进 fixes.json/eval 供离线评估）；
已修复测试变红或 golden 等价破坏 ⇒ 回滚。测试文件名/测试内容是框架受控
资产，agent 只拿到通过/失败与失败摘要（预期行为规范，真实 debug 合理）。

golden 分两段跑：先 test_bugs 归因本轮转绿集合，再跑 golden（
GOLDEN_ALLOWED_PH = 已修复 ∪ 本轮转绿），受影响场景必须与 clean 行为一致。
fail 从备份回滚；红线：agent 自写测试不参与。
"""
from __future__ import annotations

import re

from agent.config import MAX_PATCH_ATTEMPTS, TESTS_DIR
from agent.nodes.common import b_to_ph, ph_to_b, TokenDelta
from agent.tools.patch import apply_patch, revert_backup, run_pytest

ALL_BUGS = [f"B{i:02d}" for i in range(1, 13)]


def _broken_bugs(output: str) -> set[str]:
    """pytest 输出 → 失败/出错 test_Bxx 的集合（该 bug 测试未全绿）。"""
    broken: set[str] = set()
    for line in output.splitlines():
        m = re.search(r"(?:FAILED|ERROR)\s+\S*?(test_B\d+)\.py", line)
        if m:
            broken.add(m.group(1))
    return broken


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
            # 段 1：全量 test_bugs → 归因本轮转绿的 bug
            fixed_b = {ph_to_b(f["phenomenon_id"]) for f in fixed}
            tr = run_pytest([TESTS_DIR / "test_bugs"])
            broken = _broken_bugs(tr.output)
            newly = [b for b in ALL_BUGS if b not in broken and b not in fixed_b]
            regressions = sorted(fixed_b & broken)
            no_tests = "no tests ran" in tr.output

            if not no_tests and newly and not regressions:
                # 段 2：golden 等价回归（门控放行已修复 ∪ 本轮转绿）
                allowed = sorted({f["phenomenon_id"] for f in fixed}
                                 | {b_to_ph(b) for b in newly})
                tr_golden = run_pytest([TESTS_DIR / "golden"], allowed_ph=allowed)
                result["test_result"] = {
                    "ok": tr_golden.failed == 0,
                    "stage": "test",
                    "passed": tr.passed, "failed": tr.failed,
                    "skipped": tr.skipped + tr_golden.skipped,
                    "attributed": newly,
                    "output_tail": (tr.output + tr_golden.output)[-1500:],
                }
                if result["test_result"]["ok"]:
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
                hyp["last_error"] = f"golden 等价回归失败（failed={tr_golden.failed}），已回滚"
            elif regressions:
                hyp["last_error"] = f"已修复测试回归: {','.join(regressions)}，已回滚"
            elif no_tests:
                hyp["last_error"] = "pytest 未收集到任何测试（环境异常），已回滚"
            else:
                still = sorted(set(broken) - fixed_b) or ["（无失败明细）"]
                hyp["last_error"] = f"补丁未使任何受控测试转绿（仍失败: {','.join(still[:6])}）"
            result["test_result"] = {
                "ok": False, "stage": "test",
                "passed": tr.passed, "failed": tr.failed, "skipped": tr.skipped,
                "error": hyp["last_error"], "output_tail": tr.output[-1500:],
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
