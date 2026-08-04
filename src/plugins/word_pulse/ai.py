from __future__ import annotations

import json

import httpx
from nonebot import logger
from pydantic import BaseModel, Field, ValidationError


class WordPulseAIError(Exception):
    """AI 分析失败基类。"""


class WordPulseAITimeoutError(WordPulseAIError):
    """AI 请求超时。"""


class WordPulseAIAuthError(WordPulseAIError):
    """AI 鉴权失败。"""


class WordPulseAIServiceError(WordPulseAIError):
    """AI 服务异常。"""


class WordPulseAIResponseError(WordPulseAIError):
    """AI 返回格式异常。"""


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


# ── Shared HTTP helper ──


def _build_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/chat/completions"


def _extract_content(payload: dict) -> str:
    """从 OpenAI 兼容响应里取出 message.content 字符串。"""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise WordPulseAIResponseError("响应中缺少 choices")
    msg = choices[0].get("message")
    if not isinstance(msg, dict):
        raise WordPulseAIResponseError("响应中缺少 message")
    content = msg.get("content")
    if not isinstance(content, str):
        raise WordPulseAIResponseError("响应中缺少 content")
    return content


async def _request_llm(
    *, base_url: str, api_key: str, model: str,
    messages: list[dict], temperature: float, timeout_seconds: float,
) -> dict:
    """发送 OpenAI 兼容请求并返回解析后的 JSON dict。

    直接使用 ``response_format: {type: "json_object"}``，由 pydantic 在调用方
    做二次 schema 校验。原 strict json_schema + json_object 两级降级已删除，因为
    部分上游 OpenAI 兼容网关对 strict json_schema 支持不完整（首次即 400），降级
    到 json_object 后某些网关/模型同样不支持 —— bug#3。
    """
    url = _build_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body: dict = {
        "model": model,
        "temperature": temperature,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
    except httpx.TimeoutException as e:
        raise WordPulseAITimeoutError("AI 请求超时") from e
    except httpx.HTTPStatusError as e:
        raise _to_ai_error(e) from e
    except httpx.RequestError as e:
        raise WordPulseAIServiceError(f"AI 请求失败: {type(e).__name__}: {e}") from e

    try:
        payload = resp.json()
    except json.JSONDecodeError as e:
        raise WordPulseAIResponseError("AI 接口返回不是合法 JSON") from e
    if not isinstance(payload, dict):
        raise WordPulseAIResponseError("AI 接口响应格式不正确")
    content = _extract_content(payload)
    logger.debug(f"[词频统计] AI 原始输出: {content}")
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise WordPulseAIResponseError("模型输出不是合法 JSON") from e


def _to_ai_error(e: httpx.HTTPStatusError) -> WordPulseAIError:
    """把 HTTPStatusError 按状态码映射到对应的 WordPulseAI* 异常。"""
    if e.response.status_code in {401, 403}:
        return WordPulseAIAuthError("AI 鉴权失败")
    return WordPulseAIServiceError(f"AI 服务返回 HTTP {e.response.status_code}")


# ── Call 1: Charset expansion ──


_CHARSET_SYSTEM = (
    "你是中文群聊话题分类助手。给定主题与子类（cluster）种子词，"
    "为每个 cluster 列出该话题语境下语义相关的中文字符（用于粗过滤）。"
    "只返回字符（单字），不要返回词。宁可多列不可漏列。\n"
    "必须返回严格 JSON，schema 形如：\n"
    '{"charsets": [{"cluster": "种子词", "chars": ["字1", "字2", ...]}]}\n'
    "每个 cluster 的 chars 数组必须含 5-30 个字符。"
)


async def expand_charsets(
    *, base_url: str, api_key: str, model: str,
    seeds: list[str], theme: str, temperature: float = 0.0, timeout: float = 60.0,
) -> dict[str, list[str]]:
    cluster_lines = "\n".join(f"- {s}" for s in seeds)
    parsed = await _request_llm(
        base_url=base_url, api_key=api_key, model=model,
        messages=[{"role": "system", "content": _CHARSET_SYSTEM},
                  {"role": "user", "content": f"主题：{theme}\n子类种子词：\n{cluster_lines}\n\n请为每个子类列出 5-30 个语义相关的中文字符。"}],
        temperature=temperature, timeout_seconds=timeout,
    )
    try:
        validated = CharsetExpansionResponse.model_validate(parsed)
    except ValidationError as e:
        raise WordPulseAIResponseError(f"字符集扩展返回格式异常: {e}") from e
    return {item.cluster: item.chars for item in validated.charsets}


# ── Call 2: Grey-area batch classification ──


_BATCH_SYSTEM = (
    "你是中文群聊话题分类助手。给定主题与子类簇定义，"
    "把每条消息归到一个最匹配的子类或 null（表示不属于该主题）。\n"
    "子类描述中若带「别名:」后缀，表示该子类同时匹配这些别名表达，"
    "归到该子类时按等同语义处理。\n"
    "必须返回严格 JSON，schema 形如：\n"
    '{"results": [{"id": <消息id>, "cluster": "子类名" 或 null}]}\n'
    "results 数组必须为每条输入消息返回一个条目，id 与输入消息的 [id] 对应。"
)


async def classify_batch(
    *, base_url: str, api_key: str, model: str,
    messages: list[tuple[int, str]], clusters: list[dict], theme_name: str,
    temperature: float = 0.0, timeout: float = 60.0, max_batch_size: int = 1000,
) -> list[tuple[int, str | None]]:
    if not messages:
        return []
    cluster_lines = "\n".join(
        f"- {c['name']}" + (f" (别名: {', '.join(c['aliases'])})" if c.get('aliases') else "")
        for c in clusters
    )
    all_results: list[tuple[int, str | None]] = []
    for start in range(0, len(messages), max_batch_size):
        chunk = messages[start:start + max_batch_size]
        msg_lines = "\n".join(f"[{mid}] {txt}" for mid, txt in chunk)
        parsed = await _request_llm(
            base_url=base_url, api_key=api_key, model=model,
            messages=[{"role": "system", "content": _BATCH_SYSTEM},
                      {"role": "user", "content": f"主题：{theme_name}\n子类：\n{cluster_lines}\n\n消息：\n{msg_lines}"}],
            temperature=temperature, timeout_seconds=timeout,
        )
        try:
            validated = BatchClassificationResponse.model_validate(parsed)
        except ValidationError as e:
            raise WordPulseAIResponseError(f"批量分类返回格式异常: {e}") from e
        all_results.extend((item.id, item.cluster) for item in validated.results)
    return all_results


# ── Call 3: Final summary ──


_SUMMARY_SYSTEM = (
    "你是中文群聊话题热度分析助手。根据提供的日桶统计数据，"
    "给出主题讨论的趋势总结和典型原文。趋势总结 ≤ 80 字。\n"
    "必须返回严格 JSON，schema 形如：\n"
    '{"ranking": [{"cluster": "x", "count": N, "percent": M}], '
    '"trend": "≤80字趋势总结", '
    '"examples": [{"cluster": "x", "text": "原文", "author": "发言人", "day": "YYYY-MM-DD"}], '
    '"unclassified_high_freq": [{"term": "词", "count": N}]}\n'
    "examples 最多 5 条；unclassified_high_freq 最多 8 条。"
)


async def summarize(
    *, base_url: str, api_key: str, model: str,
    prompt: str, temperature: float = 0.3, timeout: float = 60.0,
) -> SummaryResult:
    parsed = await _request_llm(
        base_url=base_url, api_key=api_key, model=model,
        messages=[{"role": "system", "content": _SUMMARY_SYSTEM}, {"role": "user", "content": prompt}],
        temperature=temperature, timeout_seconds=timeout,
    )
    try:
        return SummaryResult.model_validate(parsed)
    except ValidationError as e:
        raise WordPulseAIResponseError(f"AI 总结返回格式异常: {e}") from e
