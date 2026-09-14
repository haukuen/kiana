from __future__ import annotations

import json

from nonebot import logger, require
from pydantic import BaseModel, Field

_ai = require("src.plugins.ai_provider")

CALLER = "word_pulse"


class WordPulseAIError(Exception):
    """AI 分析失败基类。"""


class WordPulseAIConfigError(WordPulseAIError):
    """AI 配置或请求参数不合法。"""


class WordPulseAITimeoutError(WordPulseAIError):
    """AI 请求超时。"""


class WordPulseAIAuthError(WordPulseAIError):
    """AI 鉴权失败。"""


class WordPulseAIServiceError(WordPulseAIError):
    """AI 服务异常。"""


class WordPulseAIResponseError(WordPulseAIError):
    """AI 返回格式异常。"""


_AI_ERRORS = _ai.AIErrorTypes(
    timeout=WordPulseAITimeoutError,
    auth=WordPulseAIAuthError,
    service=WordPulseAIServiceError,
    response=WordPulseAIResponseError,
    config=WordPulseAIConfigError,
)


class CharsetItem(BaseModel):
    cluster: str = Field(description="输入的子类种子词，须与输入对应")
    chars: list[str] = Field(
        min_length=5,
        max_length=30,
        description="该子类语境下语义相关的中文单字，每项只含一个汉字",
    )


class CharsetExpansionResponse(BaseModel):
    charsets: list[CharsetItem] = Field(description="每个输入子类对应一个字符集")


class BatchClassifyItem(BaseModel):
    id: int = Field(description="输入消息的 id")
    cluster: str | None = Field(description="最匹配的输入子类名；不属于主题时为 null")


class BatchClassificationResponse(BaseModel):
    results: list[BatchClassifyItem] = Field(
        description="每条输入消息对应一个条目，id 与输入消息一一对应"
    )


class RankItem(BaseModel):
    cluster: str = Field(description="输入的子类名")
    count: int = Field(description="该子类在统计窗口内的消息数")
    percent: float = Field(description="该子类的热度百分比")


class ExampleItem(BaseModel):
    cluster: str = Field(description="原文所属的输入子类名")
    text: str = Field(description="输入提供的典型原文，不得改写")
    author: str = Field(description="该原文的发言人")
    day: str = Field(description="该原文在输入中的日期，格式为 YYYY-MM-DD")


class UnclassifiedTerm(BaseModel):
    term: str = Field(description="输入提供的未分类高频词")
    count: int = Field(description="输入提供的该词出现次数")


class SummaryResult(BaseModel):
    ranking: list[RankItem] = Field(description="各子类按统计数据形成的热度排名")
    trend: str = Field(description="结合各日期日桶比较变化的趋势总结，不超过 80 字")
    examples: list[ExampleItem] = Field(
        max_length=5,
        description="统计窗口内的典型原文，最多 5 条",
    )
    unclassified_high_freq: list[UnclassifiedTerm] = Field(
        max_length=8,
        description=(
            "仅列出输入提供且可核对的未分类词频；缺少词频依据时返回空列表，禁止猜测，最多 8 条"
        ),
    )


async def _request_llm[T: BaseModel](
    *,
    response_model: type[T],
    system: str = "",
    messages: list[dict[str, str]],
    temperature: float,
    timeout_seconds: float,
) -> T:
    """通过前置插件请求并校验严格结构化输出。"""
    result = await _ai.complete(
        caller=CALLER,
        system=system,
        messages=messages,
        response_model=response_model,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        errors=_AI_ERRORS,
    )
    logger.debug(f"[词频统计] AI 原始输出: {result.text}")
    if result.parsed is None:  # 共享层已保证 parse 成功非空；防御兜底
        raise WordPulseAIResponseError("AI 未返回可解析的结构化结果")
    return result.parsed


# ── Call 1: Charset expansion ──


_CHARSET_SYSTEM = (
    "你是中文群聊话题分类助手。给定主题与子类（cluster）种子词，"
    "为每个 cluster 列出该话题语境下语义相关的中文字符（用于粗过滤）。"
    "每个输入 cluster 都须对应输出；只返回字符（单字），不要返回词。宁可多列不可漏列。"
)


async def expand_charsets(
    *,
    seeds: list[str],
    theme: str,
    temperature: float = 0.0,
    timeout: float = 60.0,
) -> dict[str, list[str]]:
    cluster_lines = "\n".join(f"- {s}" for s in seeds)
    validated = await _request_llm(
        response_model=CharsetExpansionResponse,
        system=_CHARSET_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": f"主题：{theme}\n子类种子词：\n{cluster_lines}\n\n请为每个子类列出 5-30 个语义相关的中文字符。",
            },
        ],
        temperature=temperature,
        timeout_seconds=timeout,
    )
    return {item.cluster: item.chars for item in validated.charsets}


# ── Call 2: Grey-area batch classification ──


_BATCH_SYSTEM = (
    "你是中文群聊话题分类助手。给定主题与子类簇定义，"
    "把每条消息归到一个最匹配的子类或 null（表示不属于该主题）。\n"
    "子类的 aliases 列出与 name 等同的别名表达，归类时返回该子类的 name。"
    "每条输入消息都须对应一个结果，id 与输入消息的 id 对应。"
)


async def classify_batch(
    *,
    messages: list[tuple[int, str]],
    clusters: list[dict],
    theme_name: str,
    temperature: float = 0.0,
    timeout: float = 60.0,
    max_batch_size: int = 1000,
) -> list[tuple[int, str | None]]:
    if not messages:
        return []
    cluster_defs = [{"name": c["name"], "aliases": c.get("aliases") or []} for c in clusters]
    all_results: list[tuple[int, str | None]] = []
    for start in range(0, len(messages), max_batch_size):
        chunk = messages[start : start + max_batch_size]
        payload = {
            "theme": theme_name,
            "clusters": cluster_defs,
            "messages": [{"id": mid, "text": text} for mid, text in chunk],
        }
        validated = await _request_llm(
            response_model=BatchClassificationResponse,
            system=_BATCH_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            temperature=temperature,
            timeout_seconds=timeout,
        )
        all_results.extend((item.id, item.cluster) for item in validated.results)
    return all_results


# ── Call 3: Final summary ──


_SUMMARY_SYSTEM = (
    "你是中文群聊话题热度分析助手。根据提供的日桶统计数据，"
    "比较各日期的讨论变化，给出主题讨论的热度排名、趋势总结和典型原文。"
    "趋势总结不超过 80 字；典型原文不得改写，发言人和日期须与输入对应。"
)


async def summarize(
    *,
    prompt: str,
    temperature: float = 0.3,
    timeout: float = 60.0,
) -> SummaryResult:
    return await _request_llm(
        response_model=SummaryResult,
        system=_SUMMARY_SYSTEM,
        messages=[
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        timeout_seconds=timeout,
    )
