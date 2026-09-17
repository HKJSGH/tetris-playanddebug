r"""vision — LLM 节点：看截图描述可见异常。

工单号关联截图与反馈段（screenshot_{ticket}_{n}.png）。无截图 → findings=[]
并记 vision_skipped。输出 observations 仅描述画面可见异常（颜色/渲染残影/
窗口位置），不涉及现象号；现象归因由 diagnostician 对照 catalog 完成。
"""
from __future__ import annotations

import base64
import json

from agent.nodes.common import TokenDelta, get_llm_for
from agent.tools.llm import (
    content_part_image_b64, content_part_text, llm_available, parse_json_text,
)

SYSTEM = (
    "你是游戏 QA 视觉分析员。逐张分析俄罗斯方块游戏截图，只描述画面中可见的异常："
    "方块颜色不一致、预览区渲染残影/多轮廓叠加、窗口位置异常等。"
    '输出 JSON 数组：[{"image": 序号, "observations": ["异常描述", ...]}]，'
    "无异常则 []。不要猜测代码原因。"
)


def _feedback_by_ticket(rd) -> dict[str, str]:
    out: dict[str, str] = {}
    block: list[str] = []
    ticket = ""
    for line in (rd.feedback_text or "").splitlines():
        if line.startswith("## "):
            if block and ticket:
                out[ticket] = "\n".join(block).strip()
            ticket = ""
            block = []
            for token in line.replace("#", " ").split():
                if token.startswith("S") and token[1:].isdigit():
                    ticket = token
            block.append(line)
        elif ticket:
            block.append(line)
    if block and ticket:
        out[ticket] = "\n".join(block).strip()
    return out


def node_vision(state: dict) -> dict:
    acc = TokenDelta()
    acc.step("vision")
    rd = state["round_data"]
    if not rd.screenshots:
        return {"vision_findings": [], "vision_skipped": True, **acc.out()}

    fb = _feedback_by_ticket(rd)
    mode = state.get("mode", "mock")
    if mode == "mock" or not llm_available("vision"):
        return {"vision_findings": [], "vision_skipped": mode == "mock", **acc.out()}

    llm = get_llm_for(mode, "vision")
    findings: list[dict] = []
    for i, shot in enumerate(rd.screenshots, start=1):
        ticket = shot.stem.split("_")[1] if "_" in shot.stem else ""
        user = {
            "role": "user",
            "content": [
                content_part_text(
                    "这是游戏截图。若玩家反馈存在，附在最下方供参考。\n"
                    + (f"玩家反馈（工单 {ticket}）：{fb.get(ticket, '（无）')}")
                ),
                content_part_image_b64(base64.b64encode(shot.read_bytes()).decode("ascii")),
            ],
        }
        try:
            result = llm.chat(
                [{"role": "system", "content": SYSTEM}, user], temperature=0.2,
            )
            acc.usage("vision", result)
            data = parse_json_text(result.text)
            if isinstance(data, list):
                findings.extend(
                    {"ticket_id": ticket, "image": str(shot.name), "observations": item.get("observations", [])}
                    for item in data if isinstance(item, dict)
                )
        except Exception as e:  # noqa: BLE001
            acc.error(f"vision: {e!r}")
    return {"vision_findings": findings, "vision_skipped": False, **acc.out()}
