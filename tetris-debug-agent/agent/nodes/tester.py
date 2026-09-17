r"""tester — 纯代码节点：应用补丁 → 跑受控测试 → 保留/回滚 → 路由。

测试子集 = 当前假设测试 ∪ 已修复现象测试（tests/test_bugs/test_Bxx.py，
文件定位经 truth_map 纯代码转换）+ tests/golden（GOLDEN_ALLOWED_PH 门控
放行当前假设与已修复项）。fail 从备份回滚，红线：agent 自写测试不参与。
"""
from __future__ import annotations

from agent.config import MAX_PATCH_ATTEMPTS, TESTS_DIR
from agent.nodes.common import ph_to_b, TokenDelta
from agent.tools.patch import apply_patch, revert_backup, run_pytest


def _test_files_for(phenomena: list[str]) -> list:
    return [TESTS_DIR / "test_bugs" / f"test_{ph_to_b(ph)}.py" for ph in phenomena]


def node_tester(state: dict) -> dict:
    acc = TokenDelta()
    acc.step("tester")
    hyp = dict(state.get("current_hypothesis") or {})
    patch = state.get("patch") or {"blocks": []}
    attempts = state.get("patch_attempts", 0) + 1
    fixed = list(state.get("fixed_phenomena") or [])
    max_att = state.get("max_patch_attempts", MAX_PATCH_ATTEMPTS)
    ph = hyp.get("phenomenon_id", "")

    result = {"patch_attempts": attempts, **acc.out()}

    blocks = patch.get("blocks") or []
    if not blocks:
        hyp["attempts"] = attempts
        hyp["last_error"] = "patcher 未产出有效补丁"
        result["current_hypothesis"] = hyp
        result["test_result"] = {"ok": False, "stage": "patch", "error": hyp["last_error"]}
    elif not ph:
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
            phenomena = [f["phenomenon_id"] for f in fixed] + [ph]
            tr = run_pytest(
                _test_files_for(phenomena) + [TESTS_DIR / "golden"],
                allowed_ph=phenomena,
            )
            result["test_result"] = {
                "ok": tr.returncode == 0 and tr.failed == 0,
                "stage": "test",
                "passed": tr.passed, "failed": tr.failed, "skipped": tr.skipped,
                "output_tail": tr.output[-1500:],
            }
            if result["test_result"]["ok"]:
                hyp["attempts"] = attempts
                fixed.append({
                    "phenomenon_id": ph,
                    "suspect_function": hyp.get("suspect_function", ""),
                    "hypothesis": hyp.get("rationale", ""),
                    "patch": {"blocks": blocks, "note": patch.get("note", "")},
                    "tests_passed": {"passed": tr.passed, "skipped": tr.skipped},
                    "attempts_used": attempts,
                })
                result.update({"fixed_phenomena": fixed, "current_hypothesis": hyp})
                return result

            revert_backup(applied.backup_path)
            hyp["attempts"] = attempts
            hyp["last_error"] = f"pytest failed={tr.failed}: {tr.output[-500:]}" or "测试未通过"
            result["current_hypothesis"] = hyp

    # 失败：未耗尽 → 回 patcher；耗尽 → 在节点内记 rejected（路由函数无法写 state）
    if attempts < max_att:
        return result
    rejected = list(state.get("rejected") or [])
    if ph and ph not in {r["phenomenon_id"] for r in rejected}:
        rejected.append({
            "phenomenon_id": ph,
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
    rejected_ph = {r["phenomenon_id"] for r in state.get("rejected", [])}
    if state.get("patch_attempts", 0) < max_att and hyp.get("phenomenon_id") \
            and hyp["phenomenon_id"] not in rejected_ph:
        return "patcher"
    # 当前假设已耗尽（记入 rejected）或无假设：找下一个可用假设
    fixed_ph = {f["phenomenon_id"] for f in state.get("fixed_phenomena", [])}
    cursor = state.get("hypothesis_cursor", -1)
    hypotheses = state.get("hypotheses") or []
    nxt = cursor + 1
    while nxt < len(hypotheses) and hypotheses[nxt]["phenomenon_id"] in rejected_ph | fixed_ph:
        nxt += 1
    if nxt < len(hypotheses) and nxt < state.get("max_hypotheses", 5):
        return "diagnostician"
    return "wrapup"
