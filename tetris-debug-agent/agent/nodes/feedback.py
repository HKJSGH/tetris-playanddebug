r"""feedback — LLM 节点：工单反馈 + errors.log + 探针摘要 → 症状清单（含分拣）。

每条症状带 kind 字段：
  bug         描述异常/损坏（「XX 坏了 / 不对 / 会穿模」）→ 进入诊断修复循环
  suggestion  提出期望/改进（「希望 / 最好能 / 建议加 XX」）→ 优化建议旁路：
              不生成假设、不打补丁，由 wrapup 直接记入 bonus_findings
分拣不对称原则：拿不准就归 bug——建议被当 bug 只是白试几轮补丁，
真缺陷被当建议就漏修了。

Mock/无 key 时兜底：探针/错误日志恒为 bug；反馈段落用关键词启发式分拣，
与反馈段落原文透传。
"""
from __future__ import annotations

import json
import re

from agent.nodes.common import TokenDelta, get_llm_for
from agent.tools.llm import llm_available, parse_json_text

SYSTEM = (
    "你是玩家反馈整理员。把玩家的文字反馈、运行错误日志与遥测探针信号整理成症状清单。\n"
    "每条症状要分类（kind）：描述异常、故障、错误现象的是 bug；提出期望、"
    "改进建议、新功能诉求的是 suggestion（如「希望能……」「建议加……」）。"
    "拿不准的一律归 bug。\n"
    '输出 JSON 数组：[{"ticket_id": "S0000001 或 probe", "text": "症状描述", '
    '"kind": "bug 或 suggestion", "source": "反馈|探针|错误日志"}]，'
    "按对游戏体验的影响排序。不要猜测代码原因。"
)

# 兜底分拣：命中期望/建议类措辞 → suggestion（启发式，宁缺勿滥）
_SUGGESTION_RE = re.compile(r"希望|最好|建议|能不能|可不可以|如果可以|不如|干脆|顺便")


def _classify(text: str) -> str:
    """关键词启发式分拣（mock/无 key 兜底用）。"""
    return "suggestion" if _SUGGESTION_RE.search(text) else "bug"


def _split_feedback(rd) -> list[dict]:
    out: list[dict] = []
    ticket, lines = "", []
    for line in (rd.feedback_text or "").splitlines():
        if line.startswith("## "):
            if lines and ticket:
                out.append({"ticket_id": ticket, "text": "\n".join(lines).strip()})
            ticket = ""
            lines = []
            for token in line.replace("#", " ").split():
                # 工单号格式 [S0000001]，剥掉方括号再判定（否则永远匹配不上）
                tok = token.strip("[]")
                if tok.startswith("S") and tok[1:].isdigit():
                    ticket = tok
        elif ticket:
            lines.append(line)
    if lines and ticket:
        out.append({"ticket_id": ticket, "text": "\n".join(lines).strip()})
    return out


def _probe_signals(report: dict) -> list[str]:
    # 只传证据文本；探针键名（Bxx）是框架内部编号，不进 LLM prompt
    return [
        f"探针：{v['evidence'][0] if v['evidence'] else v['status']}"
        for v in report.get("probes", {}).values()
        if v["status"] == "signal"
    ]


def node_feedback(state: dict) -> dict:
    acc = TokenDelta()
    acc.step("feedback")
    rd = state["round_data"]
    report = state["probe_report"]

    paragraphs = _split_feedback(rd)
    errors = [ln for ln in (rd.errors_text or "").splitlines() if ln.strip()]
    signals = _probe_signals(report)

    mode = state.get("mode", "mock")
    if mode == "mock" or not llm_available("text"):
        symptoms = [
            {"ticket_id": p["ticket_id"], "text": p["text"], "source": "反馈",
             "kind": _classify(p["text"])} for p in paragraphs
        ] + [{"ticket_id": "probe", "text": s, "source": "探针", "kind": "bug"}
             for s in signals] + [
            {"ticket_id": "error", "text": e, "source": "错误日志", "kind": "bug"}
            for e in errors
        ]
        return {"feedback_symptoms": symptoms, **acc.out()}

    llm = get_llm_for(mode, "text")
    prompt = {
        "反馈段落": paragraphs or "（无）",
        "错误日志": errors or "（无）",
        "遥测探针信号": signals or "（无）",
    }
    try:
        result = llm.chat(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
            temperature=0.2,
        )
        acc.usage("feedback", result)
        data = parse_json_text(result.text)
        symptoms = [
            {"ticket_id": str(d.get("ticket_id", "")), "text": str(d.get("text", "")),
             "source": str(d.get("source", "")),
             # kind 缺失/不合法时按不对称原则归 bug
             "kind": d.get("kind") if d.get("kind") in ("bug", "suggestion") else "bug"}
            for d in data if isinstance(d, dict)
        ] if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001
        acc.error(f"feedback: {e!r}")
        symptoms = []
    if not symptoms:
        symptoms = [
            {"ticket_id": p["ticket_id"], "text": p["text"], "source": "反馈",
             "kind": _classify(p["text"])} for p in paragraphs
        ] + [{"ticket_id": "probe", "text": s, "source": "探针", "kind": "bug"}
             for s in signals]
    return {"feedback_symptoms": symptoms, **acc.out()}
