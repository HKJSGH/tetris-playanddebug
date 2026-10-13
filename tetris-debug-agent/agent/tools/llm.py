r"""llm — 模型路由（OpenAI 兼容协议）+ Mock 降级。

路由：
  vision → qwen-vl-max  @ dashscope compatible-mode（DASHSCOPE_API_KEY）
  text   → deepseek-chat @ deepseek（DEEPSEEK_API_KEY）
无 key 时返回 MockLLM：vision 输出 "[]"（无发现）、text 输出 ""
（节点解析为空即走纯 Python 兜底），保证无 key 全图可跑。

失败语义（fail-fast 原则）：
  - 显式降级仅存在于 mock/无 key（构造 MockLLM），是合法路径；
  - live 模式下 chat 的一切失败（连接/超时/限流重试耗尽/认证/意外异常）
    统一 raise LLMError，节点不得吞掉 → 冒泡中止整局，绝不静默兜底
    （静默兜底=用假数据冒充真分析，会污染实验数据）。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent.config import (
    TEXT_API_KEY_ENV, TEXT_BASE_URL, TEXT_MODEL,
    VISION_API_KEY_ENV, VISION_BASE_URL, VISION_MODEL,
)


# ---- LLM 调用追踪（中间产物落盘，调试用）------------------------------------
# 所有 LLM 调用的唯一出入口就在本文件 chat()，在这里拦截一处即可覆盖全部节点。
# 开启后每次调用（成功或失败）都追加一行 JSON 到 data/runs/round_N/agent_trace.jsonl，
# 记录完整的输入消息与输出（含工具循环的中间消息）；vision 消息里的 base64 图片
# 替换为占位符，防止 trace 文件膨胀到几百 MB。
_TRACE: Path | None = None


def set_llm_trace(round_id: int | None, runs_root: Path | str | None = None) -> None:
    """开启/关闭 LLM 调用追踪。

    round_id=None 关闭；否则指向 data/runs/round_N/agent_trace.jsonl 并清空旧文件
    （同一局重复跑时保证 trace 只含本次运行，与 telemetry.jsonl 的清空语义一致）。
    """
    global _TRACE
    if round_id is None:
        _TRACE = None
        return
    d = Path(runs_root) / f"round_{round_id}"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "agent_trace.jsonl"
    p.write_text("", encoding="utf-8")
    _TRACE = p


def _strip_media(messages: list[dict]) -> list[dict]:
    """深拷贝消息列表，把 image_url 部件的 base64 数据替换为占位符。

    trace 是给人看的调试产物，图片本体没有阅读价值；不替换的话每张截图
    会给 trace 文件增加几 MB。tool 消息 / tool_calls 等其余字段原样保留。
    """
    out = []
    for m in messages:
        m2 = dict(m)
        c = m2.get("content")
        if isinstance(c, list):
            parts = []
            for p in c:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    parts.append({"type": "image_url", "image_url": {"url": "<base64 已省略>"}})
                else:
                    parts.append(p)
            m2["content"] = parts
        out.append(m2)
    return out


def _trace_record(tag: str, kind: str, **fields) -> None:
    """追加一条调用记录到 trace 文件；未开启或写盘失败时静默跳过。

    写盘失败不抛异常：trace 是调试辅助，绝不能反过来弄挂正式流程。
    """
    if _TRACE is None:
        return
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "tag": tag, "kind": kind, **fields}
    try:
        with _TRACE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


class LLMError(RuntimeError):
    """LLM 不可用（基础设施/认证/重试耗尽）：live 模式下节点必须让它冒泡。"""


def _is_retriable(e: Exception) -> bool:
    """瞬时性错误值得退避重试：限流/上游过载/连接抖动/超时/响应截断。

    JSONDecodeError 只可能来自 SDK 解析「200 但响应体被截断/不完整」的 HTTP
    响应（传输层故障）——模型输出的 JSON 是否合法由节点层的 parse_json_text
    负责，不经过本函数，所以这里重试安全不会掩盖输出质量问题。
    """
    name = type(e).__name__
    s = str(e)
    return (
        isinstance(e, json.JSONDecodeError)
        or "429" in s or "RateLimit" in name or "响应无内容" in s
        or "Connection" in name or "Timeout" in name or "RemoteProtocolError" in name
        or "connection" in s.lower() or "timed out" in s.lower()
    )


@dataclass
class ChatResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0        # 前缀缓存命中 tokens（评估「命中/总 prompt」用；无则 0）
    # function calling：模型发起的工具调用列表，每项 {"id","name","arguments"}
    # （arguments 保持模型返回的原始 JSON 字符串，由调用方解析）；
    # 空列表 = 模型未调工具、text 即最终回答
    tool_calls: list[dict] = field(default_factory=list)


class MockLLM:
    """无 key 降级：vision→"[]"，text→""（节点解析失败走内置兜底）。"""

    def __init__(self, role: str):
        self.role = role

    def chat(self, messages: list[dict], **kw) -> ChatResult:
        return ChatResult(text="[]" if self.role == "vision" else "")


class OpenAICompatLLM:
    # 退避等待（秒）：拉长到 10/30/90——实测 round_16 曾出现连续 3 次 180s 超时后
    # 第 4 次成功的情况，短退避窗口（3/8/15，总等待 26s）扛不住分钟级网络抖动；
    # 配合 180s 单请求超时，可吸收约 10 分钟级别的网络故障
    _RETRY_WAIT = (10, 30, 90)
    _TIMEOUT_SEC = 180          # 单次请求超时（挂死的请求快速失败进退避重试，
                                # 而不是让整个流程干等 SDK 默认的 600 秒）

    def __init__(self, model: str, base_url: str, api_key: str):
        from openai import OpenAI

        self.model = model
        # max_retries=0：重试统一由本类的退避循环管理（SDK 内部默认再重试 2 次
        # 会让一次「重试」实际最多发 3 个请求，总时长不可控）
        self._client = OpenAI(base_url=base_url, api_key=api_key,
                              timeout=self._TIMEOUT_SEC, max_retries=0)

    def chat(self, messages: list[dict], tag: str = "", **kw) -> ChatResult:
        """瞬时错误退避重试（10/30/90s，共 4 次尝试）；任何终态失败 raise LLMError。

        tag = 调用方节点标识（如 "diagnostician"），只用于 trace 落盘归类，
        不透传给 SDK。成功与最终失败（LLMError 抛出前）都写一条 trace 记录。
        """
        last: Exception | None = None
        for i in range(len(self._RETRY_WAIT) + 1):
            try:
                resp = self._client.chat.completions.create(model=self.model, messages=messages, **kw)
                choice = (resp.choices or [None])[0]
                msg = getattr(choice, "message", None)
                text = getattr(msg, "content", None)
                # function calling：模型发起的工具调用（正文为空但带 tool_calls 是合法响应，
                # 不得当「响应无内容」重试——否则工具循环会被误判上游过载反复退避）
                tool_calls = [
                    {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                    for tc in (getattr(msg, "tool_calls", None) or [])
                ]
                if not text and not tool_calls:
                    dump = str(resp.model_dump())[:300] if hasattr(resp, "model_dump") else repr(resp)[:300]
                    # OpenRouter 偶发 200 但响应体无 choices/content（上游过载），可重试
                    raise RuntimeError(f"响应无内容: {dump}")
                usage = getattr(resp, "usage", None)
                # 缓存命中 tokens：OpenAI 协议 usage.prompt_tokens_details.cached_tokens，
                # DeepSeek 官方协议 usage.prompt_cache_hit_tokens；两者取非 None 者
                details = getattr(usage, "prompt_tokens_details", None)
                cached = (getattr(details, "cached_tokens", None)
                          or getattr(usage, "prompt_cache_hit_tokens", None) or 0)
                result = ChatResult(
                    text=text or "",
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    cached_tokens=cached,
                    tool_calls=tool_calls,
                )
                _trace_record(tag, "ok", model=self.model,
                              messages=_strip_media(messages), text=result.text,
                              tool_calls=result.tool_calls,
                              usage={"prompt": result.prompt_tokens,
                                     "completion": result.completion_tokens,
                                     "cached": result.cached_tokens})
                return result
            except Exception as e:  # noqa: BLE001
                last = e
                if _is_retriable(e) and i < len(self._RETRY_WAIT):
                    time.sleep(self._RETRY_WAIT[i])
                    continue
                break
        # 重试耗尽或不可重试（认证错误/意外异常）：统一归类为 LLMError 冒泡中止；
        # 抛出前写一条 error 记录（输入消息 + 最终异常），LLMError 中止的局
        # 也能在 trace 里看到最后那次调用到底发了什么
        _trace_record(tag, "error", model=self.model, messages=_strip_media(messages),
                      error=f"{type(last).__name__}: {last}" if last else "LLM 调用失败（无异常详情）")
        raise LLMError(f"{type(last).__name__}: {last}" if last else "LLM 调用失败（无异常详情）") from last


def get_llm(role: str) -> OpenAICompatLLM | MockLLM:
    """role ∈ {"vision", "text"}；对应 key 缺失时降级 MockLLM。"""
    if role == "vision":
        key = os.environ.get(VISION_API_KEY_ENV, "")
        if key:
            return OpenAICompatLLM(VISION_MODEL, VISION_BASE_URL, key)
    else:
        key = os.environ.get(TEXT_API_KEY_ENV, "")
        if key:
            return OpenAICompatLLM(TEXT_MODEL, TEXT_BASE_URL, key)
    return MockLLM(role)


def llm_available(role: str) -> bool:
    env = VISION_API_KEY_ENV if role == "vision" else TEXT_API_KEY_ENV
    return bool(os.environ.get(env, ""))


def content_part_text(text: str) -> dict:
    return {"type": "text", "text": text}


def content_part_image_b64(image_b64: str) -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}


def parse_json_text(text: str) -> object:
    """容错解析 LLM 输出中的 JSON：剥代码围栏后从首个 { 或 [ 起 raw_decode 首个完整值。

    不能用 find/rfind 截取首尾括号：JSON 字符串值里常含代码括号（如 search 片段
    带 [-1, 0]），会把截取区间切错导致合法 JSON 解析失败。
    """
    s = text.strip()
    if "```" in s:
        s = s.split("```")[1] if s.count("```") >= 2 else s
        s = s.lstrip()
        if s.startswith("json"):
            s = s[4:].lstrip()
    dec = json.JSONDecoder()
    first_err: json.JSONDecodeError | None = None
    for i, ch in enumerate(s):
        if ch in "[{":
            try:
                obj, _end = dec.raw_decode(s, i)
                return obj
            except json.JSONDecodeError as e:
                if first_err is None:
                    first_err = e
    if first_err is not None:
        raise first_err
    return json.loads(s)
