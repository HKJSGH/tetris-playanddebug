r"""tester — 纯代码节点：应用补丁 → 跑测试判断修好了哪个 bug → 保留或回滚 → 决定下一步。

游戏注入了 12 个 bug（编号 B01..B12），tests/test_bugs/ 下每个 bug 对应
一个测试文件（test_B01.py .. test_B12.py）。判断「这次补丁修好了哪个
bug」的办法：应用补丁后跑全部 12 个测试文件，其中某个文件从失败变为
**整个文件所有用例全绿** ⇒ 这次补丁修好了那个 bug（Bxx 编号经纯代码
换算成 PH-xx 写进 fixes.json 和评估报告；agent 不知道 bug 清单，这个
判断完全由测试结果决定，不由 LLM 猜测）。

「已修好」的判断基线来自两处，两边必须用同一个集合：
  - 跨局：data/fixes.json 的 fixed_phenomena（历史局确认修复的）；
  - 本局：黑板 fixed_phenomena（本次 debug 会话已确认修复的）。
基线内的 bug 测试应当一直全绿——补丁后反而变红说明把修好的改坏了
（改坏旧账），立即回滚。
agent 侧用于判断的基线，与 tests/golden 测试的放行名单（环境变量
GOLDEN_ALLOWED_PH ∪ fixes.json）必须是同一个集合，否则会出现
「bug 测试绿了但 golden 测试因放行名单没包含而红」——把好补丁误判成
失败。

tests/golden/ 是标准行为对比测试：用干净版（无 bug）游戏录制 12 个场景
的行为快照，补丁后的游戏重放同样场景，行为必须与快照一致——目的是
拦住「修好了目标 bug 却改变了正常行为」的补丁。对比测试失败时，把
场景名和差异摘要回传给 patcher（补丁生成器），LLM 才知道「差在哪」
而不是只看到一个失败计数。
失败一律从备份回滚，游戏文件恢复补丁前状态。红线：agent 自己写的
测试不参与判断（防止自证成功）。
"""
from __future__ import annotations

import json
import re
import time

from agent.config import (
    FIXES_PATH, GIVEUP_REPEATS, MAX_REDIAG_ROUNDS, PATCH_BUDGET_PER_HYPOTHESIS,
    PATCH_BUDGET_TOTAL, TARGET_FILE, TESTS_DIR,
)
from agent.nodes.common import b_to_ph, ph_to_b, TokenDelta
from agent.tools.patch import apply_patch, revert_backup, run_pytest

ALL_BUGS = [f"B{i:02d}" for i in range(1, 13)]   # 12 个注入 bug 的编号，与 tests/test_bugs/ 文件名一一对应


# ---- 函数快照：给 data/runs/round_N/patches.md 人工阅读用，只进账本，绝不进任何 LLM prompt ----

def _enclosing_function(content: str, fragment: str) -> str:
    """在源码里定位 search 片段所属的函数。

    从片段所在位置向前逐行找最近的 def 行，返回函数名；
    片段在模块顶层（不属于任何函数）时返回 ""。
    """
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
    """从源码中按函数名取出该函数的完整文本。

    从 def <func_name>( 行开始收集，遇到缩进不深于 def 行的代码行即停
    （函数体结束）；空行照收，末尾去空行。找不到该函数返回 ""。
    变量：start = def 行的行号，indent = def 行的缩进。
    """
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
    """补丁应用成功后，截取每个被改函数的「补丁前 / 补丁后」完整源码。

    补丁前 = 备份文件（apply_patch 写盘前留下的），补丁后 = 当前目标文件；
    必须在跑测试和回滚之前调用，此刻两者才恰好是前后两个状态。
    返回 [{"function": 函数名, "before": 前源码, "after": 后源码}]，
    同一函数只截一次；读文件失败返回 []。
    """
    try:
        before = backup_path.read_text(encoding="utf-8").replace("\r\n", "\n")
        after = TARGET_FILE.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return []
    snaps: list[dict] = []
    seen: set[str] = set()      # 已截过的函数名（一个补丁可能多次改同一函数）
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
    """读 data/fixes.json，返回历史局已确认修复的 PH 编号集合。

    这是跨局的「已修好基线」；文件缺失或损坏时返回空集合（视为无历史）。
    """
    try:
        data = json.loads(FIXES_PATH.read_text(encoding="utf-8"))
        return {f["phenomenon_id"] for f in data.get("fixed_phenomena", [])}
    except Exception:  # noqa: BLE001
        return set()


def _broken_bugs(output: str) -> dict[str, set[str]]:
    """从 pytest 输出解析哪些 bug 的测试有失败用例。

    逐行匹配 FAILED/ERROR 行里的 test_Bxx.py::用例名，返回
    {测试文件名: 该文件失败用例名的集合}。判定口径：文件里有任何一个
    用例失败，这个 bug 的测试就没过（不允许「部分绿」）。
    """
    broken: dict[str, set[str]] = {}
    for line in output.splitlines():
        m = re.search(r"(?:FAILED|ERROR)\s+\S*?test_(B\d+)\.py::(\S+)", line)
        if m:
            broken.setdefault(f"test_{m.group(1)}", set()).add(m.group(2))
    return broken


def _golden_fail_summary(golden_output: str) -> str:
    """把标准行为对比测试（tests/golden）的失败输出压成一段简短摘要。

    提取失败场景名（FAILED ... [SC_Bxx]）和 pytest 的断言差异行（E 开头），
    拼成「场景 X 与干净版行为快照不一致｜差异要点: ...」，回传给 patcher，
    让 LLM 知道行为差在哪；拿不到场景明细时如实说明。
    """
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
    """构造本次补丁尝试的空白记录条目，之后按实际结果补齐字段。

    attempts = 本假设下的第几次尝试，n_blocks = 补丁块数。
    完整条目经黑板 attempt_log 汇入 wrapup → data/patch_history.jsonl
    （append-only 账本，含补丁全文与时间戳）；进 patcher prompt 的只有
    摘要投影（patcher._attempt_history，无时间戳、无补丁全文），
    账本与 prompt 两层不得混淆。
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


def _repeated_failure(problem: str, attempt_log: list[dict], current_error: str) -> int:
    """统计当前问题同一失败原因在尝试历史中出现的次数（含本次）。

    纯文本逐字比对，零 LLM 成本。次数 ≥ GIVEUP_REPEATS 说明 patcher
    在同一思路上反复产出等价补丁（原地打转），应当提前放弃当前假设。
    """
    if not problem or not current_error:
        return 0
    n = 1     # 含本次（当前条目尚未经 reducer 并入黑板 attempt_log）
    for a in attempt_log:
        if (str(a.get("problem", "")).strip() == problem
                and not a.get("ok")
                and str(a.get("error", "")) == current_error):
            n += 1
    return n


def node_tester(state: dict) -> dict:
    """tester 节点：拿 patcher 的补丁做一次完整验证，是保留还是回滚在这里定。

    五种出口：
      ① patcher 没产出补丁块 → 失败，回 patcher；
      ② 假设缺少问题描述 → 失败，回 patcher；
      ③ search 片段在游戏源码里命中失败（0 次或多次）→ 文件未动，失败；
      ④ 补丁已应用但测试没通过（bug 测试没转绿 / 基线内变红 / 对比测试失败）
         → 从备份回滚游戏文件，失败；
      ⑤ 补丁应用且全部测试通过 → 保留补丁，把修好的 bug 记入黑板 fixed_phenomena。

    所有失败出口在函数尾部统一做两件事：
      - 同一失败原因重复 ≥ GIVEUP_REPEATS 次 → 提前放弃当前假设；
      - 单假设预算用完或提前放弃 → 把假设写进黑板 rejected（路由函数无法写 state）。

    关键局部变量：
      blocks = 补丁块列表（来自 patcher 的 SEARCH/REPLACE 块）
      entry  = 本次尝试的账本条目，各出口补齐字段后由 reducer 并入黑板 attempt_log
      fixed  = 本局已确认修复的清单（写回前仅存在于局部）
      base_ph / fixed_b = 「已修好」的 PH 编号集合与对应的 Bxx 编号集合
                           （两套编号是同一批 bug 的两种记法，见 common.py）
      broken / broken_b = 跑完 bug 测试后仍失败的测试文件与对应的 Bxx 编号
      newly  = 基线之外整个文件全绿的测试 → 本次补丁修好的 bug
    """
    acc = TokenDelta()
    acc.step("tester")
    hyp = dict(state.get("current_hypothesis") or {})
    patch = state.get("patch") or {"blocks": []}
    attempts = state.get("patch_attempts", 0) + 1
    fixed = list(state.get("fixed_phenomena") or [])
    max_att = state.get("max_patch_attempts", PATCH_BUDGET_PER_HYPOTHESIS)
    problem = str(hyp.get("problem", "")).strip()

    # 全局补丁预算：本局所有假设共享，tester 是唯一计数点
    total = state.get("patch_attempts_total", 0) + 1
    result = {"patch_attempts": attempts, "patch_attempts_total": total, **acc.out()}

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
            golden_out = ""  # 对比测试只在 bug 测试判断出修复后才跑；失败路径统一取测试输出尾部
            # ---- 已修好基线：跨局（fixes.json）∪ 本局（黑板），两处必须同源 ----
            base_ph = {f["phenomenon_id"] for f in fixed} | _fixed_ph_from_fixes()
            fixed_b = {ph_to_b(ph) for ph in base_ph}

            # 段 1：跑全部 bug 测试 → 与基线对比，判断这次补丁修好了哪些 bug
            tr = run_pytest([TESTS_DIR / "test_bugs"])
            broken = _broken_bugs(tr.output)
            broken_b = {b.removeprefix("test_") for b in broken}
            regressions = sorted(fixed_b & broken_b)          # 基线内的测试变红 = 把修好的改坏了
            if regressions:
                # 跨尝试累积（eval 用真实回归数，不再硬编码 0）
                result["regressions"] = sorted(
                    set(state.get("regressions") or []) | set(regressions))
            # 只认「基线外整个文件全绿」：文件里有任何一个用例失败都不算修好
            newly = [b for b in ALL_BUGS if b not in broken_b and b not in fixed_b]
            no_tests = "no tests ran" in tr.output

            entry["stage"] = "test"
            entry["passed"], entry["failed"] = tr.passed, tr.failed
            if not no_tests and newly and not regressions:
                # 段 2：标准行为对比测试——确认这次补丁没有改变正常行为。
                # 放行名单 = 基线 ∪ 本次修好的，与 tests/golden 测试内部读到的
                # 必须是同一个集合，否则好补丁会被对比测试误判成失败
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
                msg = f"补丁未使任何受控测试转绿（仍失败: {','.join(still[:6])}）"
                # 失败信息保真：失败测试集合与上次完全相同 = 改动没碰到任何
                # 失败路径，大概率改错位置——把这一事实明确告诉 patcher
                prev = next((a for a in reversed(state.get("attempt_log") or [])
                             if str(a.get("problem", "")).strip() == problem
                             and isinstance(a.get("broken_b"), list)), None)
                if prev is not None and prev["broken_b"] == sorted(broken_b):
                    msg += ("；本次改动后失败测试集合与上次完全相同"
                            "（改动未影响任何失败测试，疑似改错位置）")
                hyp["last_error"] = msg
            entry["error"] = hyp["last_error"]
            entry["blocks"] = blocks
            entry["broken_b"] = sorted(broken_b)   # 失败测试集合快照（审计 + 跨尝试对比用）
            result["test_result"] = {
                "ok": False, "stage": "test",
                "passed": tr.passed, "failed": tr.failed, "skipped": tr.skipped,
                "error": hyp["last_error"],
                "output_tail": (tr.output + golden_out)[-2500:],
            }
            revert_backup(applied.backup_path)

    # 提前放弃当前假设：同一失败原因在该假设尝试历史中重复出现 ≥
    # GIVEUP_REPEATS 次（含本次）——patcher 原地打转的实证，不再烧完单假设
    # 预算，直接记 rejected 换下一个假设
    early_giveup = (_repeated_failure(problem, state.get("attempt_log") or [],
                                      entry.get("error", "")) >= GIVEUP_REPEATS)
    if early_giveup:
        entry["error"] += f"（同一失败原因已重复 {GIVEUP_REPEATS} 次，提前放弃当前假设）"
        hyp["last_error"] = entry["error"]
        if result.get("test_result"):
            result["test_result"]["error"] = entry["error"]
        result["current_hypothesis"] = hyp

    # 失败：单假设预算未用完且未触发提前放弃 → 回 patcher；否则在节点内记
    # rejected（路由函数无法写 state）
    if attempts < max_att and not early_giveup:
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
            # 放弃原因：预算用完（attempts_used_up）｜失败原因重复（repeated_failure）
            "stop_reason": "repeated_failure" if early_giveup else "attempts_used_up",
        })
        result["rejected"] = rejected
    return result


def route_after_tester(state: dict) -> str:
    """tester 之后的路由函数：只读黑板，决定下一个节点。

    六条路由（按判断顺序）：
      ① 补丁验证通过 → wrapup（本局告一段落，去落盘修复记录）；
      ② 全局补丁总预算用完 → wrapup（收场，不再烧任何预算）；
      ③ 当前假设的单假设预算没用完、问题有效且未被否决 → 回 patcher 换个改法再试；
      ④ 当前假设被否决（预算用完或提前放弃）→ 跳到下一个还没处理过的假设，
         交给 diagnostician；
      ⑤ 当前假设清单已检验完（没有下一假设或本批上限用完）、本局有被否决的
         失败实证、重诊断轮数没用完 → 回 diagnostician 携带实证重诊断一轮；
      ⑥ 其余情况 → wrapup 收场。

    关键变量：
      done        = 已处理过的问题文本集合（已修复的 + 已否决的），用来跳过重复假设
      batch_start = 本批假设在 hypotheses 里的起始下标，max_hyp 上限按批内相对计数
                   （重诊断追加的新假设不占旧批的名额）
    """
    tr = state.get("test_result") or {}
    if tr.get("ok"):
        return "wrapup"
    if state.get("patch_attempts_total", 0) >= state.get("max_patch_total", PATCH_BUDGET_TOTAL):
        return "wrapup"     # 全局补丁总预算用完，收场
    hyp = state.get("current_hypothesis") or {}
    max_att = state.get("max_patch_attempts", PATCH_BUDGET_PER_HYPOTHESIS)
    rejected_problems = {r.get("problem") for r in state.get("rejected", [])}
    problem = str(hyp.get("problem", "")).strip()
    if (state.get("patch_attempts", 0) < max_att
            and problem and problem not in rejected_problems):
        return "patcher"
    # 当前假设已否决（单假设预算用完或提前放弃）：找下一个可用假设
    done = ({str(f.get("hypothesis", "")).strip() for f in state.get("fixed_phenomena", [])}
            | rejected_problems) - {""}
    batch_start = state.get("hypothesis_batch_start", 0)
    cursor = state.get("hypothesis_cursor", -1)
    hypotheses = state.get("hypotheses") or []
    max_hyp = state.get("max_hypotheses", 5)
    nxt = cursor + 1
    while nxt < len(hypotheses) and str(hypotheses[nxt].get("problem", "")).strip() in done:
        nxt += 1
    if nxt < len(hypotheses) and (nxt - batch_start) < max_hyp:
        return "diagnostician"
    # 当前假设清单已检验完（全部假设处理完或本批上限用完）：本局有被否决的
    # 实证且重诊断轮数未用完 → 回 diagnostician 重诊断一轮，否则收场
    if (state.get("rediag_rounds", 0) < MAX_REDIAG_ROUNDS
            and state.get("rejected") and state.get("attempt_log")):
        return "diagnostician"   # 携带失败实证重诊断一轮
    return "wrapup"
