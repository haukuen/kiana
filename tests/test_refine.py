"""炼化插件测试。

覆盖:
- DB schema + CRUD（订阅 / 结果 / 清理 + cascade）
- collector（采集 + 目标解析 + prompt 截断）
- ai 错误分类（timeout / 401 / 5xx / 格式错）
- commands 端到端（订阅 / 查询 / 刷新 / 取消，使用 nonebug 标准模式）
"""

from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.event import Sender
from nonebug import App

# ── 事件工厂 ────────────────────────────────────────────


def make_group_event(
    message: Message | str,
    *,
    message_id: int = 1,
    user_id: int = 100001,
    group_id: int = 200001,
    self_id: int = 987654321,
    nickname: str = "测试用户",
    card: str = "",
    event_time: int | None = None,
) -> GroupMessageEvent:
    actual = message if isinstance(message, Message) else Message(message)
    return GroupMessageEvent(
        time=event_time or int(datetime.now().timestamp()),
        self_id=self_id,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        group_id=group_id,
        message_id=message_id,
        message=actual,
        original_message=actual.copy(),
        raw_message=str(actual),
        font=0,
        sender=Sender(user_id=user_id, nickname=nickname, card=card, role="member"),
    )


def configure_refine_plugin(**overrides: object) -> None:
    """在已加载的插件 config 实例上打补丁。"""
    import src.plugins.refine as rp

    for key, value in overrides.items():
        setattr(rp.config, key, value)


def _fake_ai_response(content: str = "AI 生成的总结") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"role": "assistant", "content": content}},
            ],
        },
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )


def expect_bot_not_muted(
    ctx, group_id: int = 200001, self_id: int = 987654321
) -> None:
    """should_call_send 之前必须先声明：bot 不被禁言。

    项目 ``check_group_permission`` 在群消息场景下会查 mute cache，触发
    get_group_member_info API。未先声明会导致 nonebug 报意外 API 调用。
    """
    ctx.should_call_api(
        "get_group_member_info",
        {"group_id": group_id, "user_id": self_id, "no_cache": True},
        result={"shut_up_timestamp": 0},
    )


# ── 公共 fixture ────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_refine_config() -> None:
    configure_refine_plugin(
        refine_plugin_enabled=True,
        refine_group_mode="all",
        refine_group_whitelist=[],
        refine_group_blacklist=[],
        refine_ai_base_url="https://example.com/v1",
        refine_ai_api_key="sk-test",
        refine_ai_model="gpt-test",
        refine_ai_timeout_seconds=30.0,
        refine_ai_temperature=0.3,
        refine_result_retention_days=3,
        refine_lookback_hours=24,
        refine_max_messages_per_target=200,
        refine_max_prompt_chars=12000,
        refine_min_messages_to_refine=2,
    )


# ── DB 测试 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_subscription_crud() -> None:
    from src.plugins.refine.db import (
        add_subscription,
        conflict_on_label,
        conflict_on_target,
        delete_subscription,
        get_subscription_by_label,
        list_subscriptions,
    )

    sub = await add_subscription(
        group_id="g1", target_type="user", target_value="u1", label="张三"
    )
    assert sub is not None
    assert sub.label == "张三"

    assert await conflict_on_label("g1", "张三") is not None
    assert await conflict_on_target("g1", "user", "u1") is not None
    again = await add_subscription(
        group_id="g1", target_type="user", target_value="u1", label="别名"
    )
    assert again is None

    assert len(await list_subscriptions("g1")) == 1
    assert await get_subscription_by_label("g1", "张三") is not None
    assert await get_subscription_by_label("g1", "不存在") is None

    assert await delete_subscription(group_id="g1", label="张三") is True
    assert await delete_subscription(group_id="g1", label="张三") is False
    assert len(await list_subscriptions("g1")) == 0


@pytest.mark.asyncio
async def test_db_result_purge() -> None:
    from src.plugins.refine.db import (
        add_result,
        add_subscription,
        get_latest_result,
        purge_expired_results,
    )
    from src.storage import get_db

    sub = await add_subscription(
        group_id="g1", target_type="user", target_value="u1", label="张三"
    )
    assert sub is not None

    now = int(time.time())
    new = await add_result(
        subscription_id=sub.id,
        period_start=now - 3600,
        period_end=now,
        summary="新总结",
        message_count=10,
        model_name="gpt-test",
    )
    old = await add_result(
        subscription_id=sub.id,
        period_start=now - 86400 * 30,
        period_end=now - 86400 * 30 + 3600,
        summary="远古总结",
        message_count=5,
        model_name="gpt-test",
    )

    latest = await get_latest_result(sub.id)
    assert latest is not None
    assert latest.summary == "新总结"

    # 篡改 old 的 created_at 到 10 天前，触发清理（new 保持当下）
    await get_db().execute(
        "UPDATE refine_result SET created_at = ? WHERE id = ?",
        (now - 86400 * 10, old.id),
    )
    deleted = await purge_expired_results(retention_days=3)
    assert deleted == 1
    remaining = await get_latest_result(sub.id)
    assert remaining is not None
    assert remaining.id == new.id


@pytest.mark.asyncio
async def test_db_delete_subscription_cascades_results() -> None:
    from src.plugins.refine.db import (
        add_result,
        add_subscription,
        delete_subscription,
        get_latest_result,
    )

    sub = await add_subscription(
        group_id="g1", target_type="user", target_value="u1", label="张三"
    )
    assert sub is not None
    now = int(time.time())
    await add_result(
        subscription_id=sub.id,
        period_start=now - 3600,
        period_end=now,
        summary="x",
        message_count=1,
        model_name="m",
    )
    assert await get_latest_result(sub.id) is not None

    assert await delete_subscription(group_id="g1", label="张三") is True
    assert await get_latest_result(sub.id) is None


# ── collector 测试 ─────────────────────────────────────


def test_resolve_target_variants() -> None:
    from src.plugins.refine.collector import resolve_target_type_and_value

    assert resolve_target_type_and_value("user:123456") == ("user", "123456")
    assert resolve_target_type_and_value("123456") == ("user", "123456")
    assert resolve_target_type_and_value("collection:核心") == ("collection", "核心")
    assert resolve_target_type_and_value("集合 核心") == ("collection", "核心")
    assert resolve_target_type_and_value("集合核心") == ("collection", "核心")
    assert resolve_target_type_and_value("") == (None, None)
    assert resolve_target_type_and_value("未知") == (None, None)


@pytest.mark.asyncio
async def test_collect_filters_by_user_and_window() -> None:
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.refine.collector import collect_messages_for_subscription
    from src.plugins.refine.db import RefineSubscription

    now = int(time.time())
    await archive_message_event(
        make_group_event(
            "目标发言A",
            user_id=111,
            group_id=222,
            message_id=1,
            event_time=now - 600,
            nickname="目标",
        )
    )
    await archive_message_event(
        make_group_event(
            "目标发言B",
            user_id=111,
            group_id=222,
            message_id=2,
            event_time=now - 300,
            nickname="目标",
        )
    )
    await archive_message_event(
        make_group_event(
            "他人发言", user_id=999, group_id=222, message_id=3, event_time=now - 100
        )
    )
    await archive_message_event(
        make_group_event(
            "远古发言",
            user_id=111,
            group_id=222,
            message_id=4,
            event_time=now - 86400 * 5,
        )
    )

    sub = RefineSubscription(
        id=1,
        group_id="222",
        target_type="user",
        target_value="111",
        label="目标",
        created_at=now,
    )
    collected = await collect_messages_for_subscription(
        sub, lookback_hours=24, max_messages=100, max_prompt_chars=10000
    )
    texts = [t for _, _, t in collected.messages]
    assert "目标发言A" in texts
    assert "目标发言B" in texts
    assert "他人发言" not in texts
    assert "远古发言" not in texts


@pytest.mark.asyncio
async def test_collect_handles_empty_collection_target() -> None:
    from src.plugins.refine.collector import collect_messages_for_subscription
    from src.plugins.refine.db import RefineSubscription

    now = int(time.time())
    sub = RefineSubscription(
        id=1,
        group_id="222",
        target_type="collection",
        target_value="不存在的集合",
        label="x",
        created_at=now,
    )
    collected = await collect_messages_for_subscription(
        sub, lookback_hours=24, max_messages=100, max_prompt_chars=10000
    )
    assert collected.messages == []


def test_build_prompt_payload_truncates() -> None:
    from src.plugins.refine.collector import (
        CollectedMessages,
        build_prompt_payload,
    )

    collected = CollectedMessages(
        messages=[
            (1700000000, "Alice", "x" * 50),
            (1700000100, "Bob", "y" * 50),
        ],
        period_start=1700000000,
        period_end=1700000100,
    )
    out = build_prompt_payload(collected, max_prompt_chars=80)
    assert len(out) <= 80


# ── AI 测试 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ai_success() -> None:
    from src.plugins.refine.ai import request_refine_summary

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(return_value=_fake_ai_response("hello world")),
    ):
        result = await request_refine_summary(
            base_url="https://example.com/v1",
            api_key="sk-test",
            model="gpt-test",
            timeout_seconds=10,
            temperature=0.3,
            prompt_payload="xxx",
        )
    assert result == "hello world"


@pytest.mark.asyncio
async def test_ai_timeout_classified() -> None:
    from src.plugins.refine.ai import request_refine_summary
    from src.plugins.refine.exceptions import RefineAITimeoutError

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=httpx.ReadTimeout("t")),
    ), pytest.raises(RefineAITimeoutError):
        await request_refine_summary(
            base_url="https://example.com/v1",
            api_key="sk-test",
            model="gpt-test",
            timeout_seconds=10,
            temperature=0.3,
            prompt_payload="xxx",
        )


@pytest.mark.asyncio
async def test_ai_auth_error_classified() -> None:
    from src.plugins.refine.ai import request_refine_summary
    from src.plugins.refine.exceptions import RefineAIAuthError

    response = httpx.Response(
        401,
        json={"error": "bad key"},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
        with pytest.raises(RefineAIAuthError):
            await request_refine_summary(
                base_url="https://example.com/v1",
                api_key="sk-bad",
                model="gpt-test",
                timeout_seconds=10,
                temperature=0.3,
                prompt_payload="xxx",
            )


@pytest.mark.asyncio
async def test_ai_response_format_error() -> None:
    from src.plugins.refine.ai import request_refine_summary
    from src.plugins.refine.exceptions import RefineAIResponseError

    response = httpx.Response(
        200,
        json={"foo": "bar"},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
        with pytest.raises(RefineAIResponseError):
            await request_refine_summary(
                base_url="https://example.com/v1",
                api_key="sk-test",
                model="gpt-test",
                timeout_seconds=10,
                temperature=0.3,
                prompt_payload="xxx",
            )


# ── commands 端到端（nonebug 标准模式） ────────────────


@pytest.mark.asyncio
async def test_command_subscribe_user(app: App) -> None:
    from src.plugins.refine import commands

    event = make_group_event(
        "炼化订阅 张三 user:111", message_id=10, group_id=200001
    )
    async with app.test_matcher(commands.refine_subscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            (
                "✅ 已订阅 [张三] (用户=111)\n"
                "下次定时提炼：每日 08:00\n"
                "若需立即生成结果，发送：炼化刷新 张三"
            ),
            result={"message_id": 1000},
        )


@pytest.mark.asyncio
async def test_command_subscribe_with_at(app: App) -> None:
    from src.plugins.refine import commands

    msg = Message("炼化订阅 目标A") + Message(MessageSegment.at(555))
    event = GroupMessageEvent(
        time=int(datetime.now().timestamp()),
        self_id=987654321,
        post_type="message",
        sub_type="normal",
        user_id=100001,
        message_type="group",
        group_id=200001,
        message_id=11,
        message=msg,
        original_message=msg.copy(),
        raw_message=str(msg),
        font=0,
        sender=Sender(user_id=100001, nickname="测试", card="", role="member"),
    )
    async with app.test_matcher(commands.refine_subscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            (
                "✅ 已订阅 [目标A] (用户=555)\n"
                "下次定时提炼：每日 08:00\n"
                "若需立即生成结果，发送：炼化刷新 目标A"
            ),
            result={"message_id": 1001},
        )

    from src.plugins.refine.db import get_subscription_by_label

    sub = await get_subscription_by_label("200001", "目标A")
    assert sub is not None
    assert sub.target_type == "user"
    assert sub.target_value == "555"


@pytest.mark.asyncio
async def test_command_subscribe_collection_not_exists(app: App) -> None:
    from src.plugins.refine import commands

    event = make_group_event(
        "炼化订阅 团队 collection:不存在的集合", message_id=12
    )
    async with app.test_matcher(commands.refine_subscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "集合「不存在的集合」不存在或无成员。请先用 un_nickname 插件的 `集合 <名> @人` 命令创建。",
            result={"message_id": 1002},
        )


@pytest.mark.asyncio
async def test_command_query_returns_no_result_message(app: App) -> None:
    from src.plugins.refine import commands
    from src.plugins.refine.db import add_subscription

    await add_subscription(
        group_id="200001", target_type="user", target_value="111", label="张三"
    )
    event = make_group_event("炼化查询 张三", message_id=14)
    async with app.test_matcher(commands.refine_query) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "订阅 [张三] 暂无结果。发送 `炼化刷新 张三` 立即生成",
            result={"message_id": 1003},
        )


@pytest.mark.asyncio
async def test_command_query_returns_latest_result(app: App) -> None:
    """query handler 的端到端测试因动态时间戳难以精确断言整段字符串，
    用 patch.object(commands, "datetime") 固定 strftime 输出。
    """
    from src.plugins.refine import commands
    from src.plugins.refine.db import add_result, add_subscription

    sub = await add_subscription(
        group_id="200001", target_type="user", target_value="111", label="张三"
    )
    assert sub is not None
    fixed_now = 1_800_000_000
    await add_result(
        subscription_id=sub.id,
        period_start=fixed_now - 3600,
        period_end=fixed_now,
        summary="预先存好的总结",
        message_count=10,
        model_name="gpt-test",
    )

    fake_dt = type("FakeDT", (), {"strftime": lambda self, fmt: "T"})()
    event = make_group_event("炼化查询 张三", message_id=15)

    with patch.object(commands, "datetime") as mock_dt:
        mock_dt.fromtimestamp.return_value = fake_dt
        async with app.test_matcher(commands.refine_query) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            expect_bot_not_muted(ctx)
            ctx.receive_event(bot, event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                event,
                (
                    "🧪 炼化结果：[张三]\n"
                    "目标：用户=111\n"
                    "采样窗口：T ~ T\n"
                    "采样消息：10 条\n"
                    "模型：gpt-test\n"
                    "\n"
                    "预先存好的总结"
                ),
                result={"message_id": 1004},
            )


@pytest.mark.asyncio
async def test_command_refresh_insufficient_messages(app: App) -> None:
    """刷新时窗口内目标无消息，应给出跳过提示。"""
    from src.plugins.refine import commands
    from src.plugins.refine.db import add_subscription

    await add_subscription(
        group_id="200001", target_type="user", target_value="111", label="张三"
    )
    event = make_group_event("炼化刷新 张三", message_id=16)
    async with app.test_matcher(commands.refine_refresh) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event, "⏳ 正在为 [张三] 提炼，请稍候...", result={"message_id": 1005}
        )
        ctx.should_call_send(
            event,
            "⚠️ [张三] 提炼未完成：消息不足，跳过",
            result={"message_id": 1006},
        )


@pytest.mark.asyncio
async def test_command_refresh_success(app: App) -> None:
    """刷新成功应落库并返回总结。"""
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.refine import commands
    from src.plugins.refine.db import (
        add_subscription,
        get_latest_result,
        get_subscription_by_label,
    )

    fixed_now = 1_800_000_000
    for i in range(3):
        await archive_message_event(
            make_group_event(
                f"目标发言内容{i}",
                user_id=111,
                group_id=200001,
                message_id=100 + i,
                event_time=fixed_now - 600 + i * 60,
                nickname="目标",
            )
        )

    await add_subscription(
        group_id="200001", target_type="user", target_value="111", label="张三"
    )

    event = make_group_event("炼化刷新 张三", message_id=17, event_time=fixed_now)
    fake_dt = type("FakeDT", (), {"strftime": lambda self, fmt: "T"})()
    with (
        patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(return_value=_fake_ai_response("AI 总结结果")),
        ),
        patch.object(commands, "datetime") as mock_dt,
        patch("src.plugins.refine.collector.time.time", return_value=fixed_now),
    ):
        mock_dt.fromtimestamp.return_value = fake_dt
        async with app.test_matcher(commands.refine_refresh) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            expect_bot_not_muted(ctx)
            ctx.receive_event(bot, event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                event,
                "⏳ 正在为 [张三] 提炼，请稍候...",
                result={"message_id": 1007},
            )
            ctx.should_call_send(
                event,
                (
                    "🧪 炼化结果：[张三]\n"
                    "目标：用户=111\n"
                    "采样窗口：T ~ T\n"
                    "采样消息：3 条\n"
                    "模型：gpt-test\n"
                    "\n"
                    "AI 总结结果"
                ),
                result={"message_id": 1008},
            )

    sub = await get_subscription_by_label("200001", "张三")
    assert sub is not None
    result = await get_latest_result(sub.id)
    assert result is not None
    assert result.summary == "AI 总结结果"
    assert result.message_count == 3


@pytest.mark.asyncio
async def test_command_unsubscribe(app: App) -> None:
    from src.plugins.refine import commands
    from src.plugins.refine.db import (
        add_subscription,
        get_subscription_by_label,
    )

    await add_subscription(
        group_id="200001", target_type="user", target_value="111", label="张三"
    )
    event = make_group_event("炼化取消订阅 张三", message_id=18)
    async with app.test_matcher(commands.refine_unsubscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "✅ 已取消订阅 [张三]，历史结果一并删除",
            result={"message_id": 1009},
        )

    assert await get_subscription_by_label("200001", "张三") is None


@pytest.mark.asyncio
async def test_command_list_empty(app: App) -> None:
    from src.plugins.refine import commands

    event = make_group_event("炼化订阅列表", message_id=19)
    async with app.test_matcher(commands.refine_list) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "本群暂无炼化订阅。发送 `炼化订阅 <标签> @某人` 添加",
            result={"message_id": 1010},
        )


# ── runner 单元测试（不依赖 commands） ──────────────────


@pytest.mark.asyncio
async def test_runner_skips_when_messages_insufficient() -> None:
    from src.plugins.refine.config import Config
    from src.plugins.refine.db import add_subscription
    from src.plugins.refine.runner import refine_single_subscription

    sub = await add_subscription(
        group_id="999", target_type="user", target_value="111", label="x"
    )
    assert sub is not None
    cfg = Config(
        refine_plugin_enabled=True,
        refine_ai_base_url="https://example.com/v1",
        refine_ai_api_key="sk-test",
        refine_ai_model="gpt-test",
        refine_min_messages_to_refine=100,
    )
    outcome = await refine_single_subscription(sub, cfg)
    assert not outcome.success
    assert "消息不足" in (outcome.error or "")


@pytest.mark.asyncio
async def test_runner_missing_ai_config() -> None:
    from src.plugins.refine.config import Config
    from src.plugins.refine.db import add_subscription
    from src.plugins.refine.runner import refine_single_subscription

    sub = await add_subscription(
        group_id="999", target_type="user", target_value="111", label="x"
    )
    assert sub is not None
    cfg = Config(refine_plugin_enabled=True)  # 缺 ai_*
    outcome = await refine_single_subscription(sub, cfg)
    assert not outcome.success
    assert "未配置" in (outcome.error or "")
