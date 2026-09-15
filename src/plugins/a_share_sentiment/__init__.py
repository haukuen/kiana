import time
from dataclasses import dataclass
from datetime import datetime

from nonebot import get_plugin_config, logger, on_regex, require
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.plugin import PluginMetadata

from ..group_permission import create_group_rule
from ..message_archive.db import fetch_group_messages_by_time_range
from .ai import (
    CALLER,
    SentimentAIAuthError,
    SentimentAIConfigError,
    SentimentAIResponseError,
    SentimentAIServiceError,
    SentimentAITimeoutError,
    SentimentAnalysisResult,
    request_sentiment_analysis,
)
from .analysis import (
    SHANGHAI_TZ,
    DayAnalysis,
    build_day_analysis,
    build_prompt_payload,
    get_history_day_windows,
    get_today_window,
)
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="a_share_sentiment",
    description="基于群聊归档分析当日 A 股情绪指数",
    usage="本群情绪\n今日情绪",
    config=Config,
)

config: Config = get_plugin_config(Config)
_ai = require("src.plugins.ai_provider")

a_share_sentiment_group_rule = create_group_rule(
    config_getter=lambda: config,
    plugin_enabled_attr="a_share_sentiment_plugin_enabled",
    prefix="a_share_sentiment_",
)

a_share_sentiment = on_regex(
    r"^(本群情绪|今日情绪)$",
    rule=a_share_sentiment_group_rule,
    priority=5,
    block=True,
)

cooldown_dict: dict[str, float] = {}


@dataclass(slots=True)
class CachedResult:
    created_at: float
    response_text: str


result_cache: dict[str, CachedResult] = {}


def build_cache_key(group_id: int, reference_timestamp: int) -> str:
    day_key = datetime.fromtimestamp(reference_timestamp, SHANGHAI_TZ).strftime("%Y%m%d")
    return f"{group_id}:{day_key}"


def prune_expired_cache() -> None:
    ttl_seconds = config.a_share_sentiment_cache_ttl_minutes * 60
    now = time.time()
    expired_keys = [
        key
        for key, cached_result in result_cache.items()
        if now - cached_result.created_at >= ttl_seconds
    ]
    for key in expired_keys:
        del result_cache[key]


def get_cached_result(group_id: int, reference_timestamp: int) -> str | None:
    prune_expired_cache()
    cached_result = result_cache.get(build_cache_key(group_id, reference_timestamp))
    if cached_result is None:
        return None
    return cached_result.response_text


def set_cached_result(group_id: int, reference_timestamp: int, response_text: str) -> None:
    result_cache[build_cache_key(group_id, reference_timestamp)] = CachedResult(
        created_at=time.time(),
        response_text=response_text,
    )


def get_remaining_cooldown(group_id: int) -> int:
    last_call = cooldown_dict.get(str(group_id))
    if last_call is None:
        return 0

    remaining = int(last_call + config.a_share_sentiment_cooldown_seconds - time.time())
    return max(0, remaining)


def mark_cooldown(group_id: int) -> None:
    cooldown_dict[str(group_id)] = time.time()


def validate_runtime_config() -> str | None:
    """返回配置缺失的用户可见提示；端点与模型由 ai_provider 前置插件解析。"""
    missing = _ai.resolve(CALLER).missing
    return f"A股情绪插件：{missing[0]}" if missing else None


async def fetch_analysis_context(event: GroupMessageEvent) -> tuple[DayAnalysis, list[DayAnalysis]]:
    today_window = get_today_window(event.time)
    today_messages = await fetch_group_messages_by_time_range(
        group_id=str(event.group_id),
        start_time=today_window.start_time,
        end_time=today_window.end_time,
        exclude_message_id=event.message_id,
    )
    today_analysis = build_day_analysis(
        today_messages,
        today_window.date_label,
        config.a_share_sentiment_max_today_messages,
        config.a_share_sentiment_max_prompt_chars_today,
    )

    history_analyses: list[DayAnalysis] = []
    for history_window in get_history_day_windows(
        event.time, config.a_share_sentiment_history_days
    ):
        history_messages = await fetch_group_messages_by_time_range(
            group_id=str(event.group_id),
            start_time=history_window.start_time,
            end_time=history_window.end_time,
        )
        history_analyses.append(
            build_day_analysis(
                history_messages,
                history_window.date_label,
                config.a_share_sentiment_max_history_messages_per_day,
                config.a_share_sentiment_max_prompt_chars_history_day,
            )
        )

    return today_analysis, history_analyses


def apply_low_confidence_cap(
    analysis_result: SentimentAnalysisResult,
    low_confidence: bool,
) -> SentimentAnalysisResult:
    if not low_confidence:
        return analysis_result
    return analysis_result.model_copy(update={"confidence": min(analysis_result.confidence, 0.35)})


def render_response(
    analysis_result: SentimentAnalysisResult,
    today_analysis: DayAnalysis,
    *,
    low_confidence: bool,
) -> str:
    confidence_percent = round(analysis_result.confidence * 100)
    lines = [
        f"A股情绪指数：{analysis_result.score}/100（{analysis_result.label}）",
        f"置信度：{confidence_percent}%",
        (
            f"今日样本：{today_analysis.total_messages} 条文本消息，"
            f"{today_analysis.active_users} 位活跃成员"
        ),
    ]

    if low_confidence:
        lines.append("提示：今日样本偏少，仅供参考")

    lines.append(f"总评：{analysis_result.summary}")
    lines.append("原因：")
    for index, reason in enumerate(analysis_result.reasons, start=1):
        lines.append(f"{index}. {reason}")
    lines.append(
        f"近{config.a_share_sentiment_history_days}日对比：{analysis_result.compare_to_history}"
    )
    return "\n".join(lines)


async def ensure_group_event(event: MessageEvent) -> GroupMessageEvent:
    if not isinstance(event, GroupMessageEvent):
        await a_share_sentiment.finish("仅支持群聊使用")
    return event


async def ensure_runtime_ready(group_event: GroupMessageEvent) -> None:
    config_error = validate_runtime_config()
    if config_error is not None:
        await a_share_sentiment.finish(config_error)

    cached_result = get_cached_result(group_event.group_id, group_event.time)
    if cached_result is not None:
        await a_share_sentiment.finish(cached_result)

    remaining_cooldown = get_remaining_cooldown(group_event.group_id)
    if remaining_cooldown > 0:
        await a_share_sentiment.finish(f"冷却中，请等待 {remaining_cooldown} 秒")


async def load_analysis_context_or_finish(
    group_event: GroupMessageEvent,
) -> tuple[DayAnalysis, list[DayAnalysis]]:
    try:
        today_analysis, history_analyses = await fetch_analysis_context(group_event)
    except Exception as e:
        logger.error(f"[A股情绪] 读取归档消息失败: {e}", exc_info=True)
        await a_share_sentiment.finish("读取群聊记录失败，请稍后重试")

    if today_analysis.total_messages < 1:
        await a_share_sentiment.finish("今天还没有可分析的群聊文本消息")

    return today_analysis, history_analyses


async def request_analysis_or_finish(
    *,
    prompt_payload: str,
) -> SentimentAnalysisResult:
    try:
        return await request_sentiment_analysis(
            timeout_seconds=config.a_share_sentiment_timeout_seconds,
            temperature=config.a_share_sentiment_temperature,
            prompt_payload=prompt_payload,
        )
    except SentimentAIConfigError as e:
        logger.error(f"[A股情绪] AI 配置错误: {e}")
        await a_share_sentiment.finish(f"A股情绪插件配置错误：{e}")
    except SentimentAITimeoutError:
        await a_share_sentiment.finish("A股情绪分析超时，请稍后重试")
    except SentimentAIAuthError:
        await a_share_sentiment.finish("A股情绪分析鉴权失败，请检查 API Key")
    except SentimentAIResponseError:
        await a_share_sentiment.finish("A股情绪分析返回格式异常，请检查模型输出")
    except SentimentAIServiceError as e:
        logger.error(f"[A股情绪] AI 服务异常: {e}", exc_info=True)
        await a_share_sentiment.finish("A股情绪分析服务暂时不可用，请稍后重试")
    except Exception as e:
        logger.error(f"[A股情绪] 分析失败: {e}", exc_info=True)
        await a_share_sentiment.finish("A股情绪分析失败，请稍后重试")


@a_share_sentiment.handle()
async def handle_a_share_sentiment(event: MessageEvent) -> None:
    group_event = await ensure_group_event(event)
    await ensure_runtime_ready(group_event)
    today_analysis, history_analyses = await load_analysis_context_or_finish(group_event)

    low_confidence = today_analysis.total_messages < config.a_share_sentiment_min_messages
    prompt_payload = build_prompt_payload(
        today_analysis,
        history_analyses,
        sample_insufficient=low_confidence,
    )

    mark_cooldown(group_event.group_id)
    analysis_result = await request_analysis_or_finish(prompt_payload=prompt_payload)

    response_text = render_response(
        apply_low_confidence_cap(analysis_result, low_confidence),
        today_analysis,
        low_confidence=low_confidence,
    )
    set_cached_result(group_event.group_id, group_event.time, response_text)
    await a_share_sentiment.finish(response_text)
