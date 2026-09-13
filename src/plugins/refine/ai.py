"""炼化的提示词与一次总结调用。

请求、错误映射、响应解析和端点配置都在 ``ai_provider`` 前置插件里；这里只保留
提示词，以及「把共享层的失败翻译成本插件异常」的绑定。不要求 JSON 结构化输出，
直接返回一段自然语言总结（降低对模型的提示工程依赖，便于换底层）。
"""

from __future__ import annotations

from nonebot import logger, require

from .exceptions import (
    RefineAIAuthError,
    RefineAIResponseError,
    RefineAIServiceError,
    RefineAITimeoutError,
    RefineConfigError,
)

_ai = require("src.plugins.ai_provider")

CALLER = "refine"

_AI_ERRORS = _ai.AIErrorTypes(
    timeout=RefineAITimeoutError,
    auth=RefineAIAuthError,
    service=RefineAIServiceError,
    response=RefineAIResponseError,
    config=RefineConfigError,
)


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


async def request_refine_summary(
    *,
    timeout_seconds: float,
    temperature: float,
    prompt_payload: str,
) -> str:
    """调用 AI 生成总结。失败抛 RefineAIError 子类。

    端点与模型由 ai_provider 按 ``caller="refine"`` 解析（见 ai_plugin_models）。
    """
    result = await _ai.complete(
        caller=CALLER,
        system=build_system_prompt(),
        messages=[{"role": "user", "content": build_user_prompt(prompt_payload)}],
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        errors=_AI_ERRORS,
    )
    logger.debug(f"[炼化] AI 原始输出: {result.text[:200]}")
    if not result.text:
        raise RefineAIResponseError("AI 输出内容为空")
    return result.text
