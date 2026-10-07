r"""diag_tools — diagnostician 的只读查表工具（OpenAI tools 协议）。

三个工具全部是对黑板 state 已有数据的查表，无文件系统访问、无写操作：
  read_functions(names)      读候选函数源码（与 patcher 同源的 srcmap）
  query_probe_detail(label)  查探针告警明细 + 全局统计 + 网格重建可信度
  query_events(...)          查本局原始遥测事件流（过滤查询）

泄漏红线：probe_report 的键是框架内部 bug 编号（B01..B12），任何工具的
参数、返回值与日志都不得出现。对外一律用每局临时标签 P1/P2/...；
label→内部键的映射只存在于 build_diag_tools 返回的 execute 闭包里。
同理，payload 里只列 signal 探针（列全量索引会让 LLM 数出探针总数，
暗示 bug 数量）。
"""
from __future__ import annotations

import json

from agent.config import TOOL_EVENT_LIMIT, TOOL_READ_BATCH, TOOL_SRC_CHARS

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "read_functions",
            "description": (
                "读取候选函数的完整源码。给出 suspect_function 前"
                "必须先用本工具读过该函数的源码。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": f"要阅读的函数名，1~{TOOL_READ_BATCH} 个",
                    }
                },
                "required": ["names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_probe_detail",
            "description": (
                "查询某个探针告警的完整明细（状态/置信度/全部证据条目/"
                "全局统计/网格重建可信度）。可查询的探针即输入材料"
                " probe_signals 中列出的 label。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "probe_signals 中的探针标签，如 P1",
                    }
                },
                "required": ["label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_events",
            "description": (
                "查询本局原始遥测事件流（按条件过滤，按时间倒序返回）。"
                "用于核查探针告警是否误报、核对玩家描述的行为。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event": {
                        "type": "string",
                        "description": "事件类型过滤，如 fall/move/rotate/lock/"
                                       "pause/hard_drop/preview/game_over",
                    },
                    "piece": {
                        "type": "string",
                        "description": "方块类型过滤（spawn/lock/preview 事件的 piece/kind 字段）",
                    },
                    "action": {
                        "type": "string",
                        "description": "操作方向过滤（move 事件的 action 字段，如 left/right）",
                    },
                    "last_n": {
                        "type": "integer",
                        "description": f"返回最近多少条，默认 30，上限 {TOOL_EVENT_LIMIT}",
                    },
                },
            },
        },
    },
]


def function_digest(srcmap: dict) -> list[str]:
    """签名摘要表：每行「name — def 签名 — docstring 首行」。

    给 LLM 挑选 read_functions 的阅读目标用——32 个函数只给名字时归因
    全靠名字语义猜，给签名+文档首行能把「先读什么」的命中率提上去，
    体量约为全源码的 1/10。
    """
    out = []
    for name, src in srcmap.items():
        sig = doc = ""
        for ln in src.splitlines():
            s = ln.strip()
            if not sig and s.startswith("def "):
                sig = s
                continue
            if sig and not doc:
                if s.startswith('"""') or s.startswith("'''"):
                    q = s[:3]
                    inner = s[3:]
                    if q in inner:                      # 单行 docstring
                        doc = inner.split(q)[0].strip()
                    elif inner.strip():                 # 多行 docstring 首行
                        doc = inner.strip()
                    else:
                        continue                        # 引号后换行，看下一非空行
                elif s and not s.startswith(("'''", '"""')):
                    break                               # docstring 结束（进了代码体）
            if doc:
                break
        out.append(f"{name} — {sig}{' — ' + doc[:60] if doc else ''}")
    return sorted(out)


def build_diag_tools(state: dict):
    """构造工具执行器。返回 (tools_schema, execute, probe_labels)。

    execute(name, arguments_json) -> str：任何失败（非法 JSON/未知工具/
    未知标签/参数不合法）都返回引导修正的错误文本，不抛异常——工具执行
    失败是正常对话内容，不是基础设施错误（基础设施错误由 llm.chat 的
    LLMError 覆盖）。
    probe_labels：{内部键: "P1", ...}（内部键→对外标签方向），仅 signal
    探针；给 _user_payload 渲染 probe_signals 用——对外只见标签，
    内部键不进 prompt，反向映射在 execute 闭包内反转。
    """
    srcmap = state.get("srcmap") or {}
    report = state.get("probe_report") or {}
    rd = state.get("round_data")
    events: list[dict] = list(getattr(rd, "events", None) or [])

    # signal 探针 → 临时标签（按键名排序保证标签分配确定）
    signal_keys = sorted(k for k, v in (report.get("probes") or {}).items()
                         if v.get("status") == "signal")
    # 对外方向：内部键 → 标签（payload 用）；execute 内部按标签反查内部键
    probe_labels = {k: f"P{i + 1}" for i, k in enumerate(signal_keys)}
    label_to_key = {v: k for k, v in probe_labels.items()}

    def execute(name: str, arguments: str) -> str:
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as e:
            return f"工具参数不是合法 JSON（{e}）。请传一个 JSON 对象。"
        if not isinstance(args, dict):
            return "工具参数必须是 JSON 对象。"

        if name == "read_functions":
            names = args.get("names")
            if not isinstance(names, list) or not names:
                return "参数 names 必须是非空字符串数组。"
            names = [str(n).strip() for n in names][:TOOL_READ_BATCH]
            parts, used, truncated = [], 0, False
            for n in names:
                body = srcmap.get(n)
                if body is None:
                    parts.append(f"### {n}\n（不在候选函数列表中；"
                                 "可用函数名见输入材料 candidate_functions）")
                    continue
                seg = body
                if used + len(seg) > TOOL_SRC_CHARS:
                    seg = seg[:max(0, TOOL_SRC_CHARS - used)]
                    truncated = True
                used += len(seg)
                parts.append(f"### {n}\n{seg}")
                if used >= TOOL_SRC_CHARS:
                    truncated = True
                    break
            text = "\n\n".join(parts)
            if truncated:
                text += f"\n（源码总量超过 {TOOL_SRC_CHARS} 字符上限已截断，请分批读取）"
            return text

        if name == "query_probe_detail":
            label = str(args.get("label", "")).strip()
            key = label_to_key.get(label)
            if key is None:
                # 可查询标签清单只能列对外标签（P1/P2...），列内部键=泄漏
                known = ", ".join(sorted(label_to_key)) or "（本局无 signal 探针）"
                return f"未知探针标签 {label!r}；可查询的标签: {known}"
            pr = (report.get("probes") or {}).get(key, {})
            return json.dumps({
                "label": label,
                "status": pr.get("status"),
                "confidence": pr.get("confidence"),
                "evidence": pr.get("evidence"),
                "全局统计": report.get("stats"),
                "事件计数": report.get("event_counts"),
                "网格重建可信度": (report.get("replay") or {}).get("trust"),
            }, ensure_ascii=False)

        if name == "query_events":
            ev, piece, action = (args.get("event"), args.get("piece"), args.get("action"))
            try:
                last_n = int(args.get("last_n", 30))
            except (TypeError, ValueError):
                last_n = 30
            last_n = max(1, min(last_n, TOOL_EVENT_LIMIT))
            sel = [e for e in events
                   if (not ev or e.get("event") == str(ev))
                   and (not piece or e.get("piece") == str(piece)
                        or e.get("kind") == str(piece))
                   and (not action or e.get("action") == str(action))]
            if not sel:
                kinds = ",".join(sorted({str(e.get("event", "?")) for e in events}))
                return f"（无匹配事件）本局事件类型: {kinds}"
            total = len(sel)
            lines = [json.dumps(e, ensure_ascii=False) for e in sel[-last_n:]]
            return f"匹配 {total} 条，返回最近 {len(lines)} 条（时间为正序，最后一行最新）：\n" + "\n".join(lines)

        return f"未知工具 {name!r}；可用工具: read_functions / query_probe_detail / query_events"

    return TOOLS_SCHEMA, execute, probe_labels