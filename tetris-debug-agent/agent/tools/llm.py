r"""llm — 模型路由（OpenAI 兼容协议）+ Mock 降级。

路由：
  vision → qwen-vl-max  @ dashscope compatible-mode（DASHSCOPE_API_KEY）
  text   → deepseek-chat @ deepseek（DEEPSEEK_API_KEY）
无 key 或调用失败时返回 MockLLM：vision 输出 "[]"（无发现）、text 输出 ""
（节点解析为空即走纯 Python 兜底），保证无 key 全图可跑。
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


@dataclass
class ChatResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


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
                return ChatResult(
                    text=text,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                )
            except Exception as e:  # noqa: BLE001
                retriable = "429" in str(e) or "RateLimit" in type(e).__name__ or "响应无内容" in str(e)
                if retriable and i < len(self._RETRY_WAIT):
                    time.sleep(self._RETRY_WAIT[i])
                    continue
                raise
        raise RuntimeError("unreachable")


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
