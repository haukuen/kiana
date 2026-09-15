"""A 股情绪分析的提示词、返回结构与结果解析。

请求、错误映射和响应内容提取由 ``ai_provider`` 前置插件完成；这里只保留本插件
的业务部分：提示词、严格 JSON 的返回结构，以及把共享层的失败翻译成本插件异常。
"""

from __future__ import annotations

from typing import Literal

from nonebot import logger, require
from pydantic import BaseModel, Field, field_validator

_ai = require("src.plugins.ai_provider")

CALLER = "a_share_sentiment"

_SYSTEM_PROMPT = (
    "你是审慎的 A 股群聊情绪分析助手。根据给定的群聊数据评估群内 A 股情绪，"
    "只能基于群聊内容本身判断，不要引入外部市场数据。重点关注聊天里的看多/看空措辞、"
    "追涨杀跌、连板/炸板、仓位变化、亏钱效应/赚钱效应，以及是否出现明显的 FOMO、"
    "恐慌或冷淡。根据数据中的日期区分今日与历史，并比较今日和输入中提供的历史基线。"
)


class SentimentAIError(Exception):
    """AI 分析失败基类。"""


class SentimentAIConfigError(SentimentAIError):
    """AI 配置或调用参数不合法（缺端点、temperature 冲突、response_model 不合法等）。"""


class SentimentAITimeoutError(SentimentAIError):
    """AI 请求超时。"""


class SentimentAIAuthError(SentimentAIError):
    """AI 鉴权失败。"""


class SentimentAIServiceError(SentimentAIError):
    """AI 服务异常。"""


class SentimentAIResponseError(SentimentAIError):
    """AI 返回格式异常。"""


_AI_ERRORS = _ai.AIErrorTypes(
    timeout=SentimentAITimeoutError,
    auth=SentimentAIAuthError,
    service=SentimentAIServiceError,
    response=SentimentAIResponseError,
    config=SentimentAIConfigError,
)


class SentimentAnalysisResult(BaseModel):
    score: int = Field(ge=0, le=100, description="群内 A 股情绪指数，0 最悲观，100 最乐观")
    label: Literal["极度悲观", "偏悲观", "中性", "偏乐观", "极度乐观"] = Field(
        description="与情绪指数一致的情绪等级"
    )
    confidence: float = Field(ge=0, le=1, description="基于输入样本充分程度的置信度")
    summary: str = Field(description="对今日群聊 A 股情绪的一句总评")
    reasons: list[str] = Field(description="基于群聊原文的 2 到 4 条判断原因")
    compare_to_history: str = Field(description="今日情绪相对输入中提供的历史基线的简短描述")

    @field_validator("summary", "compare_to_history")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("reasons")
    @classmethod
    def validate_reasons(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if len(normalized) < 2 or len(normalized) > 4:
            raise ValueError("reasons 长度必须在 2 到 4 之间")
        return normalized


async def request_sentiment_analysis(
    *,
    timeout_seconds: float,
    temperature: float,
    prompt_payload: str,
) -> SentimentAnalysisResult:
    """跑一次情绪分析。

    端点与模型由 ai_provider 按 ``caller="a_share_sentiment"`` 解析（见 ai_plugin_models）；
    直接传 ``SentimentAnalysisResult`` 让 SDK 官方 parse 接口完成 schema 生成、发送与
    解析（response_format.json_schema / output_config.format / text.format），
    成功即得到校验过的模型实例，不再手动二次解析。
    """
    result = await _ai.complete(
        caller=CALLER,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt_payload}],
        response_model=SentimentAnalysisResult,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        errors=_AI_ERRORS,
    )
    logger.debug(f"[A股情绪] AI 原始输出: {result.text}")
    parsed = result.parsed
    if parsed is None:  # 共享层已保证 parse 成功非空；防御兜底
        raise SentimentAIResponseError("AI 未返回可解析的结构化结果")
    return parsed
