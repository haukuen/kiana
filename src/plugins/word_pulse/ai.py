from __future__ import annotations

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
    cluster: str
    chars: list[str] = Field(min_length=5, max_length=30)


class CharsetExpansionResponse(BaseModel):
    charsets: list[CharsetItem]


class BatchClassifyItem(BaseModel):
    id: int
    cluster: str | None


class BatchClassificationResponse(BaseModel):
    results: list[BatchClassifyItem]


class RankItem(BaseModel):
    cluster: str
    count: int
    percent: float


class ExampleItem(BaseModel):
    cluster: str
    text: str
    author: str
    day: str


class UnclassifiedTerm(BaseModel):
    term: str
    count: int


class SummaryResult(BaseModel):
    ranking: list[RankItem]
    trend: str
    examples: list[ExampleItem] = Field(max_length=5)
    unclassified_high_freq: list[UnclassifiedTerm] = Field(max_length=8)


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
    "只返回字符（单字），不要返回词。宁可多列不可漏列。\n"
    "必须直接返回符合 schema 的 JSON 对象，不要使用 Markdown 代码围栏。schema 形如：\n"
    '{"charsets": [{"cluster": "种子词", "chars": ["字1", "字2", ...]}]}\n'
    "每个 cluster 的 chars 数组必须含 5-30 个字符。"
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
    "子类描述中若带「别名:」后缀，表示该子类同时匹配这些别名表达，"
    "归到该子类时按等同语义处理。\n"
    "必须直接返回符合 schema 的 JSON 对象，不要使用 Markdown 代码围栏。schema 形如：\n"
    '{"results": [{"id": <消息id>, "cluster": "子类名" 或 null}]}\n'
    "results 数组必须为每条输入消息返回一个条目，id 与输入消息的 [id] 对应。"
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
    cluster_lines = "\n".join(
        f"- {c['name']}" + (f" (别名: {', '.join(c['aliases'])})" if c.get("aliases") else "")
        for c in clusters
    )
    all_results: list[tuple[int, str | None]] = []
    for start in range(0, len(messages), max_batch_size):
        chunk = messages[start : start + max_batch_size]
        msg_lines = "\n".join(f"[{mid}] {txt}" for mid, txt in chunk)
        validated = await _request_llm(
            response_model=BatchClassificationResponse,
            system=_BATCH_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"主题：{theme_name}\n子类：\n{cluster_lines}\n\n消息：\n{msg_lines}",
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
    "给出主题讨论的趋势总结和典型原文。趋势总结 ≤ 80 字。\n"
    "必须直接返回符合 schema 的 JSON 对象，不要使用 Markdown 代码围栏。schema 形如：\n"
    '{"ranking": [{"cluster": "x", "count": N, "percent": M}], '
    '"trend": "≤80字趋势总结", '
    '"examples": [{"cluster": "x", "text": "原文", "author": "发言人", "day": "YYYY-MM-DD"}], '
    '"unclassified_high_freq": [{"term": "词", "count": N}]}\n'
    "examples 最多 5 条；unclassified_high_freq 最多 8 条。"
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
