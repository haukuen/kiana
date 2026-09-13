import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx2
from tests.ai_mock_transport import AI_HTTP_TARGET, AIHttpMock, transport_failure
import pytest
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.event import Sender
from nonebug import App


def create_group_event(
    message: Message | str,
    *,
    event_time: int | None = None,
    message_id: int = 1,
    user_id: int = 123456,
    group_id: int = 654321,
    self_id: int = 987654321,
    nickname: str = "测试用户",
) -> GroupMessageEvent:
    actual_message = message if isinstance(message, Message) else Message(message)
    return GroupMessageEvent(
        time=event_time or int(datetime.now().timestamp()),
        self_id=self_id,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        group_id=group_id,
        message_id=message_id,
        message=actual_message,
        original_message=actual_message.copy(),
        raw_message=str(actual_message),
        font=0,
        sender=Sender(user_id=user_id, nickname=nickname, card="", role="member"),
    )


def create_private_event(
    message: str,
    *,
    event_time: int | None = None,
    message_id: int = 1,
    user_id: int = 123456,
    self_id: int = 987654321,
    nickname: str = "测试用户",
) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=event_time or int(datetime.now().timestamp()),
        self_id=self_id,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=message_id,
        message=Message(message),
        original_message=Message(message),
        raw_message=message,
        font=0,
        sender=Sender(user_id=user_id, nickname=nickname, sex="unknown", age=0),
    )


def expect_bot_not_muted(ctx, group_id: int, self_id: int = 987654321) -> None:
    ctx.should_call_api(
        "get_group_member_info",
        {"group_id": group_id, "user_id": self_id, "no_cache": True},
        result={"shut_up_timestamp": 0},
    )


def configure_sentiment_plugin() -> None:
    from src.plugins.a_share_sentiment import config

    # 测试自行设置启用状态与群组规则，不依赖开发环境文件
    config.a_share_sentiment_plugin_enabled = True
    config.a_share_sentiment_group_mode = "all"
    config.a_share_sentiment_group_whitelist = []
    config.a_share_sentiment_group_blacklist = []
    # AI 端点由 ai_provider 前置插件持有，见 conftest 的 reset_ai_endpoint
    config.a_share_sentiment_history_days = 5
    config.a_share_sentiment_min_messages = 20
    config.a_share_sentiment_cooldown_seconds = 300
    config.a_share_sentiment_cache_ttl_minutes = 10
    config.a_share_sentiment_max_today_messages = 200
    config.a_share_sentiment_max_history_messages_per_day = 40
    config.a_share_sentiment_max_prompt_chars_today = 12000
    config.a_share_sentiment_max_prompt_chars_history_day = 4000


@pytest.mark.asyncio
async def test_a_share_sentiment_group_command_returns_score(app: App) -> None:
    from src.plugins.a_share_sentiment import a_share_sentiment
    from src.plugins.a_share_sentiment.ai import SentimentAnalysisResult
    from src.plugins.message_archive.db import archive_message_event

    configure_sentiment_plugin()
    base_time = int(datetime(2026, 3, 24, 14, 30).timestamp())
    for index in range(1, 4):
        await archive_message_event(
            create_group_event(
                f"A股今天有点恐慌，{600000 + index} 还在跳水",
                event_time=base_time - 1800 + index,
                message_id=index,
                user_id=10000 + index,
                group_id=654321,
                nickname=f"用户{index}",
            )
        )

    mock_result = {
        "score": 27,
        "label": "偏悲观",
        "confidence": 0.8,
        "summary": "群里整体偏谨慎，讨论集中在跳水和亏钱效应。",
        "reasons": ["多条消息提到跳水和恐慌", "用户关注仓位和止损", "几乎没有明显看多表述"],
        "compare_to_history": "相比近5日基线更悲观。",
    }

    with patch(
        "src.plugins.a_share_sentiment.request_sentiment_analysis",
        new=AsyncMock(return_value=SentimentAnalysisResult.model_validate(mock_result)),
    ):
        async with app.test_matcher(a_share_sentiment) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            event = create_group_event("本群情绪", event_time=base_time, message_id=999, group_id=654321)

            expect_bot_not_muted(ctx, group_id=654321)
            ctx.receive_event(bot, event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                event,
                (
                    "A股情绪指数：27/100（偏悲观）\n"
                    "置信度：35%\n"
                    "今日样本：3 条文本消息，3 位活跃成员\n"
                    "提示：今日样本偏少，仅供参考\n"
                    "总评：群里整体偏谨慎，讨论集中在跳水和亏钱效应。\n"
                    "原因：\n"
                    "1. 多条消息提到跳水和恐慌\n"
                    "2. 用户关注仓位和止损\n"
                    "3. 几乎没有明显看多表述\n"
                    "近5日对比：相比近5日基线更悲观。"
                ),
                result={"message_id": 1000},
            )


@pytest.mark.asyncio
async def test_a_share_sentiment_private_message_is_rejected(app: App) -> None:
    from src.plugins.a_share_sentiment import a_share_sentiment

    configure_sentiment_plugin()

    async with app.test_matcher(a_share_sentiment) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_private_event("本群情绪")

        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "仅支持群聊使用",
            result={"message_id": 1001},
        )


@pytest.mark.asyncio
async def test_a_share_sentiment_handles_no_text_messages(app: App) -> None:
    from src.plugins.a_share_sentiment import a_share_sentiment
    from src.plugins.message_archive.db import archive_message_event

    configure_sentiment_plugin()
    base_time = int(datetime(2026, 3, 24, 14, 30).timestamp())
    await archive_message_event(
        create_group_event(
            Message([MessageSegment.face(123)]),
            event_time=base_time - 60,
            message_id=1,
            group_id=654321,
        )
    )

    async with app.test_matcher(a_share_sentiment) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("本群情绪", event_time=base_time, message_id=999, group_id=654321)

        expect_bot_not_muted(ctx, group_id=654321)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "今天还没有可分析的群聊文本消息",
            result={"message_id": 1002},
        )


@pytest.mark.asyncio
async def test_a_share_sentiment_cache_hit_skips_second_ai_call(app: App) -> None:
    from src.plugins.a_share_sentiment import a_share_sentiment
    from src.plugins.a_share_sentiment.ai import SentimentAnalysisResult
    from src.plugins.message_archive.db import archive_message_event

    configure_sentiment_plugin()
    base_time = int(datetime(2026, 3, 24, 14, 30).timestamp())
    for index in range(1, 3):
        await archive_message_event(
            create_group_event(
                f"A股分歧很大，{600000 + index} 今天炸板",
                event_time=base_time - 120 + index,
                message_id=index,
                user_id=20000 + index,
                group_id=654321,
            )
        )

    mock_result = {
        "score": 42,
        "label": "中性",
        "confidence": 0.2,
        "summary": "群里观点分歧较大。",
        "reasons": ["有人看多也有人担忧炸板", "讨论热度一般"],
        "compare_to_history": "和近5日基线接近。",
    }

    with patch(
        "src.plugins.a_share_sentiment.request_sentiment_analysis",
        new=AsyncMock(return_value=SentimentAnalysisResult.model_validate(mock_result)),
    ) as mocked_request:
        async with app.test_matcher(a_share_sentiment) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            first_event = create_group_event("本群情绪", event_time=base_time, message_id=999, group_id=654321)
            second_event = create_group_event("今日情绪", event_time=base_time + 60, message_id=1000, group_id=654321)

            expect_bot_not_muted(ctx, group_id=654321)
            ctx.receive_event(bot, first_event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                first_event,
                (
                    "A股情绪指数：42/100（中性）\n"
                    "置信度：20%\n"
                    "今日样本：2 条文本消息，2 位活跃成员\n"
                    "提示：今日样本偏少，仅供参考\n"
                    "总评：群里观点分歧较大。\n"
                    "原因：\n"
                    "1. 有人看多也有人担忧炸板\n"
                    "2. 讨论热度一般\n"
                    "近5日对比：和近5日基线接近。"
                ),
                result={"message_id": 1003},
            )

            ctx.receive_event(bot, second_event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                second_event,
                (
                    "A股情绪指数：42/100（中性）\n"
                    "置信度：20%\n"
                    "今日样本：2 条文本消息，2 位活跃成员\n"
                    "提示：今日样本偏少，仅供参考\n"
                    "总评：群里观点分歧较大。\n"
                    "原因：\n"
                    "1. 有人看多也有人担忧炸板\n"
                    "2. 讨论热度一般\n"
                    "近5日对比：和近5日基线接近。"
                ),
                result={"message_id": 1004},
            )

    assert mocked_request.await_count == 1


@pytest.mark.asyncio
async def test_a_share_sentiment_cooldown_blocks_cache_miss(app: App) -> None:
    from src.plugins.a_share_sentiment import a_share_sentiment, cooldown_dict

    configure_sentiment_plugin()
    with patch("src.plugins.a_share_sentiment.time.time", return_value=1_000_000.0):
        cooldown_dict["654321"] = 1_000_000.0

        async with app.test_matcher(a_share_sentiment) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            event = create_group_event("本群情绪", group_id=654321)

            expect_bot_not_muted(ctx, group_id=654321)
            ctx.receive_event(bot, event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                event,
                "冷却中，请等待 300 秒",
                result={"message_id": 1005},
            )


@pytest.mark.asyncio
async def test_fetch_group_messages_by_time_range_respects_day_boundaries() -> None:
    from src.plugins.message_archive.db import (
        archive_message_event,
        fetch_group_messages_by_time_range,
    )

    march_23_235959 = int(datetime(2026, 3, 23, 23, 59, 59).timestamp())
    march_24_000000 = int(datetime(2026, 3, 24, 0, 0, 0).timestamp())
    march_24_120000 = int(datetime(2026, 3, 24, 12, 0, 0).timestamp())
    march_25_000000 = int(datetime(2026, 3, 25, 0, 0, 0).timestamp())

    await archive_message_event(
        create_group_event("前一天", event_time=march_23_235959, message_id=1, group_id=654321)
    )
    await archive_message_event(
        create_group_event("当天零点", event_time=march_24_000000, message_id=2, group_id=654321)
    )
    await archive_message_event(
        create_group_event("当天中午", event_time=march_24_120000, message_id=3, group_id=654321)
    )
    await archive_message_event(
        create_group_event("第二天零点", event_time=march_25_000000, message_id=4, group_id=654321)
    )

    messages = await fetch_group_messages_by_time_range(
        group_id="654321",
        start_time=march_24_000000,
        end_time=march_25_000000,
    )

    assert [message.plain_text for message in messages] == ["当天零点", "当天中午"]


def test_build_day_analysis_prioritizes_keywords_and_codes() -> None:
    from src.plugins.a_share_sentiment.analysis import build_day_analysis
    from src.plugins.message_archive.db import ArchivedMessage

    messages = [
        ArchivedMessage(
            id=1,
            session_type="group",
            session_id="654321",
            message_id=1,
            event_time=1,
            self_id="1",
            user_id="10001",
            group_id="654321",
            sender_name="用户A",
            message_cq="普通闲聊1",
            plain_text="普通闲聊1",
        ),
        ArchivedMessage(
            id=2,
            session_type="group",
            session_id="654321",
            message_id=2,
            event_time=2,
            self_id="1",
            user_id="10002",
            group_id="654321",
            sender_name="用户B",
            message_cq="600000 今天炸板了",
            plain_text="600000 今天炸板了",
        ),
        ArchivedMessage(
            id=3,
            session_type="group",
            session_id="654321",
            message_id=3,
            event_time=3,
            self_id="1",
            user_id="10003",
            group_id="654321",
            sender_name="用户C",
            message_cq="普通闲聊2",
            plain_text="普通闲聊2",
        ),
        ArchivedMessage(
            id=4,
            session_type="group",
            session_id="654321",
            message_id=4,
            event_time=4,
            self_id="1",
            user_id="10004",
            group_id="654321",
            sender_name="用户D",
            message_cq="A股今天情绪修复",
            plain_text="A股今天情绪修复",
        ),
        ArchivedMessage(
            id=5,
            session_type="group",
            session_id="654321",
            message_id=5,
            event_time=5,
            self_id="1",
            user_id="10005",
            group_id="654321",
            sender_name="用户E",
            message_cq="普通闲聊3",
            plain_text="普通闲聊3",
        ),
    ]

    analysis = build_day_analysis(messages, "2026-03-24", max_messages=3, char_budget=500)

    assert analysis.total_messages == 5
    assert analysis.keyword_messages == 2
    assert analysis.code_messages == 1
    assert analysis.sampled_messages[0].endswith("600000 今天炸板了")
    assert analysis.sampled_messages[1].endswith("A股今天情绪修复")
    assert len(analysis.sampled_messages) == 3


@pytest.mark.asyncio
async def test_request_sentiment_analysis_success() -> None:
    from src.plugins.a_share_sentiment.ai import request_sentiment_analysis

    response = httpx2.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "score": 61,
                                "label": "偏乐观",
                                "confidence": 0.66,
                                "summary": "群里整体偏乐观。",
                                "reasons": ["讨论集中在反弹", "看多措辞明显"],
                                "compare_to_history": "比近5日基线更积极。",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        },
        request=httpx2.Request("POST", "https://example.com/v1/chat/completions"),
    )

    with patch(AI_HTTP_TARGET, new=AIHttpMock(lambda request: response)):
        result = await request_sentiment_analysis(
            timeout_seconds=30,
            temperature=0.2,
            prompt_payload="{}",
        )

    assert result.score == 61
    assert result.label == "偏乐观"


@pytest.mark.asyncio
async def test_request_sentiment_analysis_rejects_non_json_content() -> None:
    from src.plugins.a_share_sentiment.ai import (
        SentimentAIResponseError,
        request_sentiment_analysis,
    )

    response = httpx2.Response(
        200,
        json={"choices": [{"message": {"content": "不是 JSON"}}]},
        request=httpx2.Request("POST", "https://example.com/v1/chat/completions"),
    )

    with (
        patch(AI_HTTP_TARGET, new=AIHttpMock(lambda request: response)),
        pytest.raises(SentimentAIResponseError),
    ):
        await request_sentiment_analysis(
            timeout_seconds=30,
            temperature=0.2,
            prompt_payload="{}",
        )


@pytest.mark.asyncio
async def test_request_sentiment_analysis_rejects_missing_fields() -> None:
    from src.plugins.a_share_sentiment.ai import (
        SentimentAIResponseError,
        request_sentiment_analysis,
    )

    response = httpx2.Response(
        200,
        json={"choices": [{"message": {"content": '{"score": 12, "label": "偏悲观"}'}}]},
        request=httpx2.Request("POST", "https://example.com/v1/chat/completions"),
    )

    with (
        patch(AI_HTTP_TARGET, new=AIHttpMock(lambda request: response)),
        pytest.raises(SentimentAIResponseError),
    ):
        await request_sentiment_analysis(
            timeout_seconds=30,
            temperature=0.2,
            prompt_payload="{}",
        )


@pytest.mark.asyncio
async def test_request_sentiment_analysis_handles_timeout() -> None:
    from src.plugins.a_share_sentiment.ai import SentimentAITimeoutError, request_sentiment_analysis

    with (
        patch(AI_HTTP_TARGET, new=AIHttpMock(transport_failure(httpx2.ReadTimeout("timeout")))),
        pytest.raises(SentimentAITimeoutError),
    ):
        await request_sentiment_analysis(
            timeout_seconds=30,
            temperature=0.2,
            prompt_payload="{}",
        )


@pytest.mark.asyncio
async def test_request_sentiment_analysis_handles_auth_error() -> None:
    from src.plugins.a_share_sentiment.ai import SentimentAIAuthError, request_sentiment_analysis

    response = httpx2.Response(
        401,
        request=httpx2.Request("POST", "https://example.com/v1/chat/completions"),
    )

    with (
        patch(AI_HTTP_TARGET, new=AIHttpMock(lambda request: response)),
        pytest.raises(SentimentAIAuthError),
    ):
        await request_sentiment_analysis(
            timeout_seconds=30,
            temperature=0.2,
            prompt_payload="{}",
        )


@pytest.mark.asyncio
async def test_request_sentiment_analysis_handles_server_error() -> None:
    from src.plugins.a_share_sentiment.ai import SentimentAIServiceError, request_sentiment_analysis

    response = httpx2.Response(
        500,
        request=httpx2.Request("POST", "https://example.com/v1/chat/completions"),
    )

    with (
        patch(AI_HTTP_TARGET, new=AIHttpMock(lambda request: response)),
        pytest.raises(SentimentAIServiceError),
    ):
        await request_sentiment_analysis(
            timeout_seconds=30,
            temperature=0.2,
            prompt_payload="{}",
        )


# ── 三种固定协议 + auto 的消费者端到端链路 ────────────────


_VALID_RESULT = {
    "score": 61,
    "label": "偏乐观",
    "confidence": 0.66,
    "summary": "群里整体偏乐观。",
    "reasons": ["讨论集中在反弹", "看多措辞明显"],
    "compare_to_history": "比近5日基线更积极。",
}

_PROTOCOL_REPLIES = {
    "success": {
        "openai_chat": {
            "choices": [
                {"message": {"content": json.dumps(_VALID_RESULT, ensure_ascii=False)}, "finish_reason": "stop"}
            ]
        },
        "anthropic_messages": {
            "content": [{"type": "text", "text": json.dumps(_VALID_RESULT, ensure_ascii=False)}],
            "stop_reason": "end_turn",
        },
        "openai_responses": {
            "object": "response",
            "status": "completed",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(_VALID_RESULT, ensure_ascii=False)}]}
            ],
        },
    },
    "refusal": {
        "openai_chat": {"choices": [{"message": {"content": None, "refusal": "无法协助该请求"}, "finish_reason": "stop"}]},
        "anthropic_messages": {"content": [{"type": "refusal", "refusal": "无法协助该请求"}], "stop_reason": "refusal"},
        "openai_responses": {
            "object": "response",
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "无法协助该请求"}]}],
        },
    },
    "truncated": {
        "openai_chat": {"choices": [{"message": {"content": "部分"}, "finish_reason": "length"}]},
        "anthropic_messages": {"content": [{"type": "text", "text": "部分"}], "stop_reason": "max_tokens"},
        "openai_responses": {
            "object": "response",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
        },
    },
}


def _install_protocol_provider(protocol: str) -> None:
    from src.plugins.ai_provider.config import config as ai_config
    from src.plugins.ai_provider.service import reset_auto_cache

    ai_config.ai_providers = [
        {
            "id": "fake",
            "protocol": protocol,
            "base_url": "https://example.com/v1",
            "api_key": "sk-test",
            "models": [],
        }
    ]
    reset_auto_cache()


def _protocol_handler(mode: str, protocol: str):
    """按协议返回成功/拒绝/截断响应；auto 协议下 chat 与 messages 端点先 404。"""

    def handler(request):
        url = str(request.url)
        if protocol == "auto" and (url.endswith("/chat/completions") or url.endswith("/messages")):
            return httpx2.Response(404, request=httpx2.Request("POST", url))
        return httpx2.Response(
            200,
            json=_PROTOCOL_REPLIES[mode]["openai_responses" if protocol == "auto" else protocol],
            request=httpx2.Request("POST", url),
        )

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages", "openai_responses", "auto"])
async def test_sentiment_consumer_chain_succeeds_on_every_protocol(protocol: str) -> None:
    from src.plugins.a_share_sentiment.ai import request_sentiment_analysis

    _install_protocol_provider(protocol)
    with patch(AI_HTTP_TARGET, new=AIHttpMock(_protocol_handler("success", protocol))):
        result = await request_sentiment_analysis(
            timeout_seconds=30,
            temperature=0.2,
            prompt_payload="{}",
        )

    assert result.score == 61
    assert result.label == "偏乐观"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages", "openai_responses", "auto"])
@pytest.mark.parametrize("mode", ["refusal", "truncated"])
async def test_sentiment_consumer_maps_rejection_and_truncation(mode: str, protocol: str) -> None:
    """拒绝与截断必须严格失败，并映射成本插件的 SentimentAIResponseError。"""
    from src.plugins.a_share_sentiment.ai import SentimentAIResponseError, request_sentiment_analysis

    _install_protocol_provider(protocol)
    with (
        patch(AI_HTTP_TARGET, new=AIHttpMock(_protocol_handler(mode, protocol))),
        pytest.raises(SentimentAIResponseError),
    ):
        await request_sentiment_analysis(
            timeout_seconds=30,
            temperature=0.2,
            prompt_payload="{}",
        )


@pytest.mark.asyncio
async def test_sentiment_temperature_conflict_maps_to_plugin_config_error() -> None:
    """合法配置 temperature=1.5 撞上 Anthropic send 策略：注入的配置异常，不是裸 AIConfigError。"""
    from src.plugins.a_share_sentiment.ai import (
        SentimentAIConfigError,
        request_sentiment_analysis,
    )
    from src.plugins.ai_provider.config import config as ai_config
    from src.plugins.ai_provider.service import reset_auto_cache

    ai_config.ai_providers = [
        {
            "id": "fake",
            "protocol": "anthropic_messages",
            "base_url": "https://example.com/v1",
            "api_key": "sk-test",
            "temperature_policy": "send",
            "models": [],
        }
    ]
    reset_auto_cache()
    with pytest.raises(SentimentAIConfigError, match="0 到 1"):
        await request_sentiment_analysis(
            timeout_seconds=30,
            temperature=1.5,
            prompt_payload="{}",
        )


def _strip_descriptions(node: object) -> object:
    """保留 schema 结构，以区分字段约束和描述中的提示文本。"""
    if isinstance(node, dict):
        return {key: ("<desc>" if key == "description" else _strip_descriptions(value)) for key, value in node.items()}
    if isinstance(node, list):
        return [_strip_descriptions(item) for item in node]
    return node


@pytest.mark.parametrize("protocol", ["anthropic_messages", "openai_chat"])
async def test_sentiment_wire_schema_preserves_sdk_constraint_contract(protocol: str) -> None:
    """业务模型经真实消费者调用，验证两种 SDK 的约束转换。"""
    from src.plugins.a_share_sentiment.ai import SentimentAnalysisResult, request_sentiment_analysis

    _install_protocol_provider(protocol)
    http = AIHttpMock(_protocol_handler("success", protocol))
    with patch(AI_HTTP_TARGET, new=http):
        result = await request_sentiment_analysis(
            timeout_seconds=30, temperature=0.2, prompt_payload="{}",
        )

    assert result == SentimentAnalysisResult(**_VALID_RESULT)
    body = http.bodies()[0]
    if protocol == "anthropic_messages":
        schema = body["output_config"]["format"]["schema"]
        dumped = json.dumps(_strip_descriptions(schema))
        for key in ("minimum", "maximum", "multipleOf", "minLength", "maxLength", "maxItems"):
            assert key not in dumped
        assert "minimum: 0" in schema["properties"]["score"].get("description", "")
        assert "maximum: 100" in schema["properties"]["score"].get("description", "")
    else:
        schema = body["response_format"]["json_schema"]["schema"]
        assert body["response_format"]["json_schema"]["strict"] is True
        assert schema["properties"]["score"]["minimum"] == 0
        assert schema["properties"]["score"]["maximum"] == 100
    assert schema["additionalProperties"] is False


def test_sentiment_config_no_longer_owns_endpoint() -> None:
    from src.plugins.a_share_sentiment.config import Config

    assert not {
        "a_share_sentiment_base_url", "a_share_sentiment_api_key", "a_share_sentiment_model",
    } & Config.model_fields.keys()
