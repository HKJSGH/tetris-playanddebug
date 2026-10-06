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
from dataclasses import dataclass

from agent.config import (
    TEXT_API_KEY_ENV, TEXT_BASE_URL, TEXT_MODEL,
    VISION_API_KEY_ENV, VISION_BASE_URL, VISION_MODEL,
)


class LLMError(RuntimeError):
    """LLM 不可用（基础设施/认证/重试耗尽）：live 模式下节点必须让它冒泡。"""


def _is_retriable(e: Exception) -> bool:
    """瞬时性错误值得退避重试：限流/上游过载/连接抖动/超时。"""
    name = type(e).__name__
    s = str(e)
    return (
        "429" in s or "RateLimit" in name or "响应无内容" in s
        or "Connection" in name or "Timeout" in name
        or "connection" in s.lower() or "timed out" in s.lower()
    )


@dataclass
class ChatResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0        # 前缀缓存命中 tokens（评估「命中/总 prompt」用；无则 0）


class MockLLM:
    """无 key 降级：vision→"[]"，text→""（节点解析失败走内置兜底）。"""

    def __init__(self, role: str):
        self.role = role

    def chat(self, messages: list[dict], **kw) -> ChatResult:
        return ChatResult(text="[]" if self.role == "vision" else "")


class OpenAICompatLLM:
    _RETRY_WAIT = (3, 8, 15)    # OpenRouter 上游 429/过载常见，指数退避重试

    def __init__(self, model: str, base_url: str, api_key: str):
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(base_url=base_url, api_key=api_key)

    def chat(self, messages: list[dict], **kw) -> ChatResult:
        """瞬时错误退避重试（3/8/15s，共 4 次尝试）；任何终态失败 raise LLMError。"""
        last: Exception | None = None
        for i in range(len(self._RETRY_WAIT) + 1):
            try:
                resp = self._client.chat.completions.create(model=self.model, messages=messages, **kw)
                choice = (resp.choices or [None])[0]
                text = getattr(getattr(choice, "message", None), "content", None)
                if not text:
                    dump = str(resp.model_dump())[:300] if hasattr(resp, "model_dump") else repr(resp)[:300]
                    # OpenRouter 偶发 200 但响应体无 choices/content（上游过载），可重试
                    raise RuntimeError(f"响应无内容: {dump}")
                usage = getattr(resp, "usage", None)
                # 缓存命中 tokens：OpenAI 协议 usage.prompt_tokens_details.cached_tokens，
                # DeepSeek 官方协议 usage.prompt_cache_hit_tokens；两者取非 None 者
                details = getattr(usage, "prompt_tokens_details", None)
                cached = (getattr(details, "cached_tokens", None)
                          or getattr(usage, "prompt_cache_hit_tokens", None) or 0)
                return ChatResult(
                    text=text,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    cached_tokens=cached,
                )
            except Exception as e:  # noqa: BLE001
                last = e
                if _is_retriable(e) and i < len(self._RETRY_WAIT):
                    time.sleep(self._RETRY_WAIT[i])
                    continue
                break
        # 重试耗尽或不可重试（认证错误/意外异常）：统一归类为 LLMError 冒泡中止
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
