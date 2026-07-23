"""OpenAI 兼容 chat/completions 调用。

参考 ``src/plugins/a_share_sentiment/ai.py``，但简化：本插件不要求 JSON 结构化
输出，直接返回一段自然语言总结即可（降低对模型的提示工程依赖，便于换底层）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from nonebot import logger

from .exceptions import (
    RefineAIAuthError,
    RefineAIResponseError,
    RefineAIServiceError,
    RefineAITimeoutError,
)


def build_chat_completions_url(base_url: str) -> str:
    """把 base_url 拼成 chat/completions 端点。

    兼容两种填法：``https://api.openai.com/v1`` 与
    ``https://api.openai.com/v1/``，都得到 ``.../v1/chat/completions``。
    """
    return f"{base_url.rstrip('/')}/chat/completions"


def build_system_prompt() -> str:
    return (
        "你是一名擅长从聊天记录中提炼信息的助理。"
        "用户会给你某个群聊目标的近期发言原文，请生成一份简明中文总结："
        "（1）该目标讨论的核心话题；（2）表达过的观点或态度；"
        "（3）值得关注的具体信息（链接、数字、承诺、计划）。"
        "只基于给定原文，不要编造未出现的内容；信息不足时直接说明。"
        "输出纯文本，不要 Markdown 标题，不要额外解释，300 字以内。"
    )


def build_user_prompt(prompt_payload: str) -> str:
    return f"目标近期发言原文如下：\n{prompt_payload}"


def extract_response_content(payload: dict[str, Any]) -> str:
    """从 OpenAI chat/completions 响应中抽出 message.content 字符串。

    兼容 string content 和 OpenAI 新版 array content（text 段拼接）。
    """
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RefineAIResponseError("响应中缺少 choices")
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise RefineAIResponseError("响应中的 choice 格式不正确")
    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise RefineAIResponseError("响应中缺少 message")
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        text_parts = [
            item["text"]
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
        if text_parts:
            return "".join(text_parts).strip()
    raise RefineAIResponseError("响应中缺少可解析的 content")


async def request_refine_summary(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    temperature: float,
    prompt_payload: str,
) -> str:
    """调用 AI 生成总结。失败抛 RefineAIError 子类。"""
    request_url = build_chat_completions_url(base_url)
    request_body = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": build_user_prompt(prompt_payload)},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
            response = await client.post(
                request_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=request_body,
            )
            response.raise_for_status()
    except httpx.TimeoutException as e:
        raise RefineAITimeoutError("AI 请求超时") from e
    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        if status_code in {401, 403}:
            raise RefineAIAuthError("AI 鉴权失败") from e
        raise RefineAIServiceError(f"AI 服务返回 HTTP {status_code}") from e
    except httpx.RequestError as e:
        raise RefineAIServiceError(f"AI 请求失败: {e}") from e

    try:
        payload = response.json()
    except json.JSONDecodeError as e:
        raise RefineAIResponseError("AI 接口返回的不是合法 JSON") from e

    if not isinstance(payload, dict):
        raise RefineAIResponseError("AI 接口响应格式不正确")

    content = extract_response_content(payload)
    logger.debug(f"[炼化] AI 原始输出: {content[:200]}")
    if not content:
        raise RefineAIResponseError("AI 输出内容为空")
    return content
