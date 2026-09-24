r"""vision — LLM 节点：玩家截图 vs 正确渲染基准图，定位并对比异常。

工单号关联截图与反馈段（screenshot_{ticket}_{n}.png）。有基准图
（data/reference/manifest.json + 模块 png，gen_reference 产出）时，每张玩家
截图一次调用同时给全部基准模块图，两步指令（先定位可疑区域，再与基准对比）；
无基准图时静默降级为旧的逐张单图描述模式。无截图 → findings=[] 并记
vision_skipped。observations 只描述画面可见差异，不涉及现象号；现象归因由
diagnostician 对照 catalog 完成。
"""
from __future__ import annotations

import base64
import json

from agent.config import REFERENCE_DIR
from agent.nodes.common import TokenDelta, get_llm_for
from agent.tools.llm import (
    content_part_image_b64, content_part_text, llm_available, parse_json_text,
)

MAX_REF_IMAGES = 5  # 单次调用的基准图上限（超出截断，防多图限流）

SYSTEM = (
    "你是游戏 QA 视觉分析员，负责把玩家截图与正确版本的基准截图对照找异常。"
    "输入是一组图片：图1 为玩家截图，其后为若干张基准模块图（图例标注区域）。"
    "两步执行："
    "第一步：逐区域检查玩家截图（游戏区 / 「下一个」预览区 / 右侧信息栏 / 暂停面板 / 整体），列出可疑区域；"
    "第二步：把每个可疑区域与对应基准图对比，只报告与基准确实存在的可见差异"
    "（颜色不一致、渲染残影/多轮廓叠加、区域布局异常等），与基准一致的不要报。"
    "注意：截图与基准反映的游戏时刻不同是正常的——分数数值、已堆叠方块的分布与颜色组合、"
    "当前下落方块的种类与位置差异都不是渲染异常，一律不要报；"
    "只有渲染本身出错（同一区域出现两套叠画的轮廓、方块颜色与应有颜色不符、区域错位/缺失）才算异常。"
    "「下一个」预览区正常只显示一个方块（同一颜色的一组格子）；若预览区叠画多个方块或多种颜色混杂，是渲染异常。"
    "你判定为非渲染异常的差异不要写进输出；没有异常就输出 []。"
    "每条异常描述以区域名开头（如「预览区：…」）。不要猜测代码原因。"
    '输出 JSON 数组：[{"image": "玩家截图文件名", "region": "主要异常区域",'
    ' "observations": ["预览区：……", ...]}]，无差异则 []。'
)

SYSTEM_LEGACY = (
    "你是游戏 QA 视觉分析员。逐张分析俄罗斯方块游戏截图，只描述画面中可见的异常："
    "方块颜色不一致、预览区渲染残影/多轮廓叠加、窗口位置异常等。"
    '输出 JSON 数组：[{"image": 序号, "observations": ["异常描述", ...]}]，'
    "无异常则 []。不要猜测代码原因。"
)


def _load_reference() -> list[dict]:
    """读基准模块清单（manifest + png）；任何缺失/损坏 → []（静默降级）。"""
    try:
        manifest = json.loads((REFERENCE_DIR / "manifest.json").read_text(encoding="utf-8"))
        refs = []
        for m in manifest.get("modules", [])[:MAX_REF_IMAGES]:
            p = REFERENCE_DIR / m.get("file", "")
            if p.exists():
                refs.append({"name": m.get("name", ""), "label": m.get("label", ""), "path": p})
        return refs if refs else []
    except Exception:  # noqa: BLE001 — 基准目录未生成是合法状态
        return []


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


def _shot_b64(shot) -> str:
    return base64.b64encode(shot.read_bytes()).decode("ascii")


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
    refs = _load_reference()
    findings: list[dict] = []
    for shot in rd.screenshots:
        ticket = shot.stem.split("_")[1] if "_" in shot.stem else ""
        fb_text = fb.get(ticket, "（无）")
        if refs:
            # 多图对比模式：图1=玩家截图，其后按 manifest 顺序为基准模块图
            legend = f"图例：图1=玩家截图（工单 {ticket}）" + "".join(
                f"；图{i + 2}=基准·{r['label']}" for i, r in enumerate(refs)
            )
            content = [
                content_part_text(
                    legend
                    + f"\n玩家反馈（工单 {ticket}）：{fb_text}"
                    + "\n按系统指令两步执行，输出 JSON。"
                ),
                content_part_image_b64(_shot_b64(shot)),
                *[content_part_image_b64(_shot_b64(r["path"])) for r in refs],
            ]
            system = SYSTEM
        else:
            # 降级：无基准图，逐张单图描述
            content = [
                content_part_text(
                    "这是游戏截图。若玩家反馈存在，附在最下方供参考。\n"
                    + (f"玩家反馈（工单 {ticket}）：{fb_text}")
                ),
                content_part_image_b64(_shot_b64(shot)),
            ]
            system = SYSTEM_LEGACY
        try:
            result = llm.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": content}],
                temperature=0.2,
            )
            acc.usage("vision", result)
            data = parse_json_text(result.text)
            if isinstance(data, list):
                findings.extend(
                    {
                        "ticket_id": ticket,
                        "image": str(shot.name),
                        "region": str(item.get("region", "")) if isinstance(item, dict) else "",
                        "observations": item.get("observations", []),
                    }
                    for item in data if isinstance(item, dict)
                )
        except Exception as e:  # noqa: BLE001
            acc.error(f"vision: {e!r}")
    return {"vision_findings": findings, "vision_skipped": False, **acc.out()}
