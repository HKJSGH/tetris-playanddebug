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
import time

from agent.config import FIXES_PATH, MAX_PATCH_ATTEMPTS, TARGET_FILE, TESTS_DIR
from agent.nodes.common import b_to_ph, ph_to_b, TokenDelta
from agent.tools.patch import apply_patch, revert_backup, run_pytest

ALL_BUGS = [f"B{i:02d}" for i in range(1, 13)]


# ---- 函数快照（patches.md 人工审计用；只进账本，绝不进任何 LLM prompt） ----

def _enclosing_function(content: str, fragment: str) -> str:
    """search 片段所在位置向前扫描，取最近的 def 函数名（模块顶层返回 ""）。"""
    idx = content.find(fragment)
    if idx < 0:
        return ""
    start_line = content.count("\n", 0, idx)
    lines = content.splitlines()
    for i in range(min(start_line, len(lines) - 1), -1, -1):
        m = re.match(r"^(\s*)def\s+(\w+)", lines[i])
        if m:
            return m.group(2)
    return ""


def _extract_function(content: str, func_name: str) -> str:
    """按函数名提取完整源码（def 行起，到下一个同级缩进行 / EOF）。"""
    if not func_name:
        return ""
    lines = content.splitlines()
    start = indent = None
    for i, ln in enumerate(lines):
        m = re.match(rf"^(\s*)def\s+{re.escape(func_name)}\s*\(", ln)
        if m:
            start, indent = i, m.group(1)
            break
    if start is None:
        return ""
    body = [lines[start]]
    width = len(indent)
    for ln in lines[start + 1:]:
        if not ln.strip():
            body.append(ln)
            continue
        if len(ln) - len(ln.lstrip()) <= width:
            break
        body.append(ln)
    return "\n".join(body).rstrip() + "\n"


def _func_snapshots(backup_path, blocks: list[dict]) -> list[dict]:
    """补丁应用成功后立即捕获：每个被改函数的补丁前（备份）/补丁后（目标）完整源码。

    必须在任何 pytest/回滚之前调用——此刻备份与目标文件恰好构成前后两个状态。
    """
    try:
        before = backup_path.read_text(encoding="utf-8").replace("\r\n", "\n")
        after = TARGET_FILE.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return []
    snaps: list[dict] = []
    seen: set[str] = set()
    for b in blocks:
        name = _enclosing_function(before, str(b.get("search", "")).replace("\r\n", "\n"))
        key = name or "<模块顶层>"
        if key in seen:
            continue
        seen.add(key)
        snaps.append({
            "function": key,
            "before": _extract_function(before, name) if name else "",
            "after": _extract_function(after, name) if name else "",
        })
    return snaps


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


def _attempt_entry(state: dict, hyp: dict, attempts: int, n_blocks: int) -> dict:
    """本次尝试的基础记录（各失败/成功出口补齐细节后由 node_tester 返回）。

    完整条目经黑板 attempt_log 汇入 wrapup → data/patch_history.jsonl（L1 审计
    账本，含补丁全文与时间戳）；进 patcher prompt 的只有 _attempt_history 的
    摘要投影（无时间戳/无补丁全文），两层不得混淆。
    """
    return {
        "round_id": state.get("round_id"),
        "mode": state.get("mode", "mock"),
        "problem": str(hyp.get("problem", "")).strip(),
        "suspect_function": hyp.get("suspect_function", ""),
        "attempt": attempts,
        "n_blocks": n_blocks,
        "patch_error": str(state.get("patch_error", "") or ""),
        "applied": False,
        "stage": "patch",
        "ok": False,
        "passed": None,
        "failed": None,
        "error": "",
        "blocks": [],
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


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
    entry = _attempt_entry(state, hyp, attempts, len(blocks))
    result["attempt_log"] = [entry]        # 各出口统一更新此条目（reducer append 进黑板）
    if not blocks:
        hyp["attempts"] = attempts
        hyp["last_error"] = "patcher 未产出有效补丁"
        entry["error"] = hyp["last_error"]
        result["current_hypothesis"] = hyp
        result["test_result"] = {"ok": False, "stage": "patch", "error": hyp["last_error"]}
    elif not problem:
        hyp["attempts"] = attempts
        hyp["last_error"] = "无有效假设"
        entry["error"] = hyp["last_error"]
        result["current_hypothesis"] = hyp
        result["test_result"] = {"ok": False, "stage": "hypothesis", "error": hyp["last_error"]}
    else:
        applied = apply_patch(blocks)
        entry["applied"] = applied.ok
        entry["stage"] = "apply"
        if not applied.ok:
            hyp["attempts"] = attempts
            hyp["last_error"] = applied.error
            entry["error"] = applied.error
            result["current_hypothesis"] = hyp
            result["test_result"] = {"ok": False, "stage": "apply", "error": applied.error}
        else:
            entry["func_snapshots"] = _func_snapshots(applied.backup_path, blocks)
            golden_out = ""  # golden 只在归因成功后运行；失败路径统一取尾部输出
            # ---- 归因基线：跨局（fixes.json）∪ 本局（黑板），两处同源 ----
            base_ph = {f["phenomenon_id"] for f in fixed} | _fixed_ph_from_fixes()
            fixed_b = {ph_to_b(ph) for ph in base_ph}

            # 段 1：全量 test_bugs → 与基线对比归因
            tr = run_pytest([TESTS_DIR / "test_bugs"])
            broken = _broken_bugs(tr.output)
            broken_b = {b.removeprefix("test_") for b in broken}
            regressions = sorted(fixed_b & broken_b)          # 基线内变红 = 修坏旧账
            if regressions:
                # 跨尝试累积（eval 用真实回归数，不再硬编码 0）
                result["regressions"] = sorted(
                    set(state.get("regressions") or []) | set(regressions))
            # 归因只认「基线外整文件全绿」（文件内任一用例红都不算）
            newly = [b for b in ALL_BUGS if b not in broken_b and b not in fixed_b]
            no_tests = "no tests ran" in tr.output

            entry["stage"] = "test"
            entry["passed"], entry["failed"] = tr.passed, tr.failed
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
                    entry["ok"] = True
                    entry["error"] = f"已修复并归因: {','.join(newly)}"
                    entry["blocks"] = blocks
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
            entry["error"] = hyp["last_error"]
            entry["blocks"] = blocks
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
        probe_key = str(hyp.get("probe_key", "") or "")
        rejected.append({
            "problem": problem,
            "suspect_function": hyp.get("suspect_function", ""),
            # 内部弱归因（评估账本用；有探针来源才填，否则诚实留空）
            "phenomenon_id": b_to_ph(probe_key) if probe_key else "",
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
