r"""feedback — LLM 节点：工单反馈 + errors.log + 探针摘要 → 症状清单。

Mock/无 key 时兜底：把反馈段落与探针 signal 项直接作为症状原样透传。
"""
from __future__ import annotations

import json

from agent.nodes.common import TokenDelta, get_llm_for
from agent.tools.llm import llm_available, parse_json_text

SYSTEM = (
    "你是玩家反馈整理员。把玩家的文字反馈、运行错误日志与遥测探针信号整理成症状清单。\n"
    '输出 JSON 数组：[{"ticket_id": "S0000001 或 probe", "text": "症状描述", "source": "反馈|探针|错误日志"}]，'
    "按对游戏体验的影响排序。不要猜测代码原因。"
)


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
                if token.startswith("S") and token[1:].isdigit():
                    ticket = token
        elif ticket:
            lines.append(line)
    if lines and ticket:
        out.append({"ticket_id": ticket, "text": "\n".join(lines).strip()})
    return out


def _probe_signals(report: dict) -> list[str]:
    return [
        f"[探针 {k}] {v['evidence'][0] if v['evidence'] else v['status']}"
        for k, v in report.get("probes", {}).items()
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
            {"ticket_id": p["ticket_id"], "text": p["text"], "source": "反馈"} for p in paragraphs
        ] + [{"ticket_id": "probe", "text": s, "source": "探针"} for s in signals] + [
            {"ticket_id": "error", "text": e, "source": "错误日志"} for e in errors
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
             "source": str(d.get("source", ""))}
            for d in data if isinstance(d, dict)
        ] if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001
        acc.error(f"feedback: {e!r}")
        symptoms = []
    if not symptoms:
        symptoms = [
            {"ticket_id": p["ticket_id"], "text": p["text"], "source": "反馈"} for p in paragraphs
        ] + [{"ticket_id": "probe", "text": s, "source": "探针"} for s in signals]
    return {"feedback_symptoms": symptoms, **acc.out()}
