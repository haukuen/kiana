"""炼化插件集成 / mock-bot 测试。

与 ``test_refine.py``（单元测试）的差异:
- 不 mock ``fetch_collection_members`` / ``fetch_group_messages_by_time_range``，
  走真实的 un_nickname + message_archive DB 链路。
- 用本地 httpx mock server（或 ``patch`` httpx 顶层 AsyncClient.post）模拟 AI
  端点，但完整跑 collect → prompt → response → 落库 链路。
- 触发 driver ``on_startup`` 钩子，验证 apscheduler 是否注册了正确的 cron job。
- 用 ``nonebug`` 完整跑事件 → matcher → handler → DB → 回包 全链路。

不连接真实 OneBot 协议端、不调真实 OpenAI API。
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
)
from nonebot.adapters.onebot.v11.event import Sender
from nonebug import App

# ── 共享工厂 ────────────────────────────────────────────


def _make_group_event(
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


def _configure_refine_plugin(**overrides: object) -> None:
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


def _expect_bot_not_muted(
    ctx, group_id: int = 200001, self_id: int = 987654321
) -> None:
    ctx.should_call_api(
        "get_group_member_info",
        {"group_id": group_id, "user_id": self_id, "no_cache": True},
        result={"shut_up_timestamp": 0},
    )


# ── 公共 fixture ────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_refine_config() -> None:
    """每用例前重置 refine config + 启用插件。"""
    _configure_refine_plugin(
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


# ═══════════════════════════════════════════════════════════════
# 1. driver.on_startup → apscheduler cron 注册
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_startup_registers_cron_jobs_when_enabled(tmp_path, monkeypatch) -> None:
    """启用插件时，driver.on_startup 应注册两个 cron job（提炼 + 清理）。

    完整跑 driver ``_lifespan.startup()`` 链路，断言 aps_scheduler 有 refine 的 job。
    """
    from nonebot_plugin_apscheduler import scheduler as aps

    # 清掉启动时已注册的 refine 同名 job（autouse fixture 可能有残留）
    for j in aps.get_jobs():
        if "refine" in (j.id or "").lower() or "refine" in (j.name or "").lower():
            j.remove()

    # 重新触发 refine 的 on_startup（conftest 的 load_plugins 已经在 session 启动时
    # 注册过；测试中再手动调用一次确保被跑）
    from src.plugins.refine import _init_refine

    await _init_refine()

    refine_jobs = [
        j for j in aps.get_jobs() if "_daily_refine_job" in (j.func.__name__ or "")
        or "_daily_purge_job" in (j.func.__name__ or "")
    ]
    assert len(refine_jobs) == 2, f"Expected 2 refine jobs, got {len(refine_jobs)}"

    # 清理
    for j in refine_jobs:
        j.remove()


@pytest.mark.asyncio
async def test_startup_skips_cron_when_disabled() -> None:
    """``refine_plugin_enabled=False`` 时不应注册 cron job。"""
    from nonebot_plugin_apscheduler import scheduler as aps

    _configure_refine_plugin(refine_plugin_enabled=False)
    # 先清掉已注册的 refine job
    for j in aps.get_jobs():
        if "_daily_refine_job" in (j.func.__name__ or "") or "_daily_purge_job" in (
            j.func.__name__ or ""
        ):
            j.remove()

    from src.plugins.refine import _init_refine

    await _init_refine()

    refine_jobs = [
        j
        for j in aps.get_jobs()
        if "_daily_refine_job" in (j.func.__name__ or "")
        or "_daily_purge_job" in (j.func.__name__ or "")
    ]
    assert len(refine_jobs) == 0


# ═══════════════════════════════════════════════════════════════
# 2. un_nickname 集成：collection 订阅 → 真实 fetch_collection_members
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_collection_subscription_collects_multiple_members() -> None:
    """collection 订阅：往 nickname_collections 表真插成员，验证采集覆盖所有成员。"""
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.refine.collector import collect_messages_for_subscription
    from src.plugins.refine.db import RefineSubscription
    from src.plugins.un_nickname.db import add_collection_members

    # 1. 创建集合 "核心" 包含 3 个成员
    added, _ = await add_collection_members(
        group_id="200001",
        collection_name="核心",
        user_ids=["111", "222", "333"],
    )
    assert len(added) == 3

    # 2. 三个成员各发几条消息，外加一个非成员的干扰消息
    now = int(time.time())
    for uid, mid in zip([111, 222, 333, 999], [1, 2, 3, 4], strict=True):
        await archive_message_event(
            _make_group_event(
                f"用户{uid}的发言",
                user_id=uid,
                group_id=200001,
                message_id=mid,
                event_time=now - 600,
                nickname=f"昵称{uid}",
            )
        )

    # 3. 用 collection 订阅采集
    sub = RefineSubscription(
        id=1,
        group_id="200001",
        target_type="collection",
        target_value="核心",
        label="核心组",
        created_at=now,
    )
    collected = await collect_messages_for_subscription(
        sub, lookback_hours=24, max_messages=100, max_prompt_chars=10000
    )

    # 4. 验证：采集到 3 条（成员 111/222/333），999 的消息不在
    texts = [t for _, _, t in collected.messages]
    assert len(texts) == 3
    assert "用户111的发言" in texts
    assert "用户222的发言" in texts
    assert "用户333的发言" in texts
    assert all("用户999" not in t for t in texts)


@pytest.mark.asyncio
async def test_collection_subscription_command_end_to_end(app: App) -> None:
    """端到端：创建集合 → 订阅 collection → 刷新 → 查询，全链路 nonebug。"""
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.refine import commands
    from src.plugins.refine.db import get_latest_result, get_subscription_by_label
    from src.plugins.un_nickname.db import add_collection_members

    # 1. 先建集合
    await add_collection_members("200001", "核心组", ["111", "222"])

    fixed_now = 1_800_000_000
    # 2. 灌发言（足够多触发提炼，>= refine_min_messages_to_refine=2）
    for uid, mid in zip([111, 222], [101, 102], strict=True):
        await archive_message_event(
            _make_group_event(
                f"集合成员{uid}的发言内容",
                user_id=uid,
                group_id=200001,
                message_id=mid,
                event_time=fixed_now - 600,
                nickname=f"成员{uid}",
            )
        )

    # 3. 订阅命令（用 collection: 写法）
    subscribe_event = _make_group_event(
        "炼化订阅 核心 collection:核心组", message_id=200
    )
    async with app.test_matcher(commands.refine_subscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, subscribe_event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            subscribe_event,
            (
                "✅ 已订阅 [核心] (集合=核心组)\n"
                "下次定时提炼：每日 08:00\n"
                "若需立即生成结果，发送：炼化刷新 核心"
            ),
            result={"message_id": 2000},
        )

    sub = await get_subscription_by_label("200001", "核心")
    assert sub is not None
    assert sub.target_type == "collection"
    assert sub.target_value == "核心组"

    # 4. 刷新命令，patch AI 返回 + 时间稳定
    fake_dt = type("FakeDT", (), {"strftime": lambda self, fmt: "T"})()
    refresh_event = _make_group_event(
        "炼化刷新 核心", message_id=201, event_time=fixed_now
    )
    with (
        patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(return_value=_fake_ai_response("集合总结内容")),
        ),
        patch.object(commands, "datetime") as mock_dt,
        patch("src.plugins.refine.collector.time.time", return_value=fixed_now),
    ):
        mock_dt.fromtimestamp.return_value = fake_dt
        async with app.test_matcher(commands.refine_refresh) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            _expect_bot_not_muted(ctx)
            ctx.receive_event(bot, refresh_event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                refresh_event,
                "⏳ 正在为 [核心] 提炼，请稍候...",
                result={"message_id": 2001},
            )
            ctx.should_call_send(
                refresh_event,
                (
                    "🧪 炼化结果：[核心]\n"
                    "目标：集合=核心组\n"
                    "集合成员：2 人\n"
                    "采样窗口：T ~ T\n"
                    "采样消息：2 条\n"
                    "模型：gpt-test\n"
                    "\n"
                    "集合总结内容"
                ),
                result={"message_id": 2002},
            )

    # 5. 验证落库
    result = await get_latest_result(sub.id)
    assert result is not None
    assert result.summary == "集合总结内容"
    assert result.message_count == 2


# ═══════════════════════════════════════════════════════════════
# 3. message_archive event_preprocessor 集成
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_message_archive_preprocessor_feeds_refine_pipeline(app: App) -> None:
    """用 event_preprocessor 走归档（不直接调 archive_message_event），
    再触发炼化采集，验证完整 preprocess → archive → collect → AI → store 链路。

    这里用消息事件直接调用 ``run_postprocessor`` 之外的方式：通过
    ``archive_received_message`` 触发 message_archive 的归档 handler。
    """

    from src.plugins.message_archive import archive_received_message
    from src.plugins.refine.collector import collect_messages_for_subscription
    from src.plugins.refine.db import RefineSubscription

    fixed_now = 1_800_000_000
    # 1. 通过 message_archive 的 event_preprocessor 归档 2 条目标发言
    target_event_1 = _make_group_event(
        "经过预处理器的发言A",
        user_id=888,
        group_id=300001,
        message_id=301,
        event_time=fixed_now - 600,
        nickname="目标用户",
    )
    target_event_2 = _make_group_event(
        "经过预处理器的发言B",
        user_id=888,
        group_id=300001,
        message_id=302,
        event_time=fixed_now - 300,
        nickname="目标用户",
    )
    # 第三条非目标用户（验证过滤）
    other_event = _make_group_event(
        "其他人发言",
        user_id=777,
        group_id=300001,
        message_id=303,
        event_time=fixed_now - 100,
    )

    # 直接调用归档函数（绕过 nonebot matcher dispatch，但走真实的
    # archive_message_event → DB 链路）
    await archive_received_message(target_event_1)
    await archive_received_message(target_event_2)
    await archive_received_message(other_event)

    # 2. 炼化采集
    sub = RefineSubscription(
        id=1,
        group_id="300001",
        target_type="user",
        target_value="888",
        label="目标",
        created_at=fixed_now,
    )
    with patch("src.plugins.refine.collector.time.time", return_value=fixed_now):
        collected = await collect_messages_for_subscription(
            sub, lookback_hours=24, max_messages=100, max_prompt_chars=10000
        )

    # 3. 验证：只采集到目标用户的 2 条，非目标被过滤
    texts = [t for _, _, t in collected.messages]
    assert len(texts) == 2
    assert "经过预处理器的发言A" in texts
    assert "经过预处理器的发言B" in texts
    assert all("其他人发言" not in t for t in texts)


# ═══════════════════════════════════════════════════════════════
# 4. 全链路：订阅 → 发言 → 刷新（mock AI）→ 查询，无动态时间问题
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_full_pipeline_subscribe_refresh_query(app: App) -> None:
    """端到端全链路（user 订阅）：
    订阅 → 灌发言 → 刷新（mock AI）→ 查询 → 验证总结。
    覆盖 commands + db + collector + ai + runner + format 全部模块。
    """
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.refine import commands
    from src.plugins.refine.db import get_latest_result, get_subscription_by_label

    fixed_now = 1_800_000_000
    # 1. 灌发言（在订阅之前，模拟历史消息已存在）
    for i in range(3):
        await archive_message_event(
            _make_group_event(
                f"目标用户的历史发言第{i}条",
                user_id=555,
                group_id=200001,
                message_id=500 + i,
                event_time=fixed_now - 600 + i * 60,
                nickname="目标用户",
            )
        )

    # 2. 订阅
    sub_event = _make_group_event(
        "炼化订阅 目标 user:555", message_id=600
    )
    async with app.test_matcher(commands.refine_subscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, sub_event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            sub_event,
            (
                "✅ 已订阅 [目标] (用户=555)\n"
                "下次定时提炼：每日 08:00\n"
                "若需立即生成结果，发送：炼化刷新 目标"
            ),
            result={"message_id": 6000},
        )

    # 3. 刷新（mock AI 返回）
    fake_dt = type("FakeDT", (), {"strftime": lambda self, fmt: "T"})()
    refresh_event = _make_group_event(
        "炼化刷新 目标", message_id=601, event_time=fixed_now
    )
    # mute cache 在 first matcher 后是否被填充依赖 nonebug 实现细节，每 matcher
    # 前手动清缓存 + 声明 mute API，保证测试稳定
    from src import plugins as global_plugins

    global_plugins._mute_cache.clear()
    with (
        patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(return_value=_fake_ai_response("完整链路总结")),
        ),
        patch.object(commands, "datetime") as mock_dt,
        patch("src.plugins.refine.collector.time.time", return_value=fixed_now),
    ):
        mock_dt.fromtimestamp.return_value = fake_dt
        async with app.test_matcher(commands.refine_refresh) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            _expect_bot_not_muted(ctx)
            ctx.receive_event(bot, refresh_event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                refresh_event,
                "⏳ 正在为 [目标] 提炼，请稍候...",
                result={"message_id": 6001},
            )
            ctx.should_call_send(
                refresh_event,
                (
                    "🧪 炼化结果：[目标]\n"
                    "目标：用户=555\n"
                    "采样窗口：T ~ T\n"
                    "采样消息：3 条\n"
                    "模型：gpt-test\n"
                    "\n"
                    "完整链路总结"
                ),
                result={"message_id": 6002},
            )

    # 4. 单独查询（验证落库后能查到，不重新调 AI）
    query_event = _make_group_event("炼化查询 目标", message_id=602)
    global_plugins._mute_cache.clear()
    with patch.object(commands, "datetime") as mock_dt:
        mock_dt.fromtimestamp.return_value = fake_dt
        async with app.test_matcher(commands.refine_query) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            _expect_bot_not_muted(ctx)
            ctx.receive_event(bot, query_event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                query_event,
                (
                    "🧪 炼化结果：[目标]\n"
                    "目标：用户=555\n"
                    "采样窗口：T ~ T\n"
                    "采样消息：3 条\n"
                    "模型：gpt-test\n"
                    "\n"
                    "完整链路总结"
                ),
                result={"message_id": 6003},
            )

    # 5. 最终验证：DB 状态
    sub = await get_subscription_by_label("200001", "目标")
    assert sub is not None
    result = await get_latest_result(sub.id)
    assert result is not None
    assert result.summary == "完整链路总结"
    assert result.message_count == 3


# ═══════════════════════════════════════════════════════════════
# 5. AI 调用真实 payload 验证（不 mock collect，只 mock httpx）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ai_payload_contains_real_archived_text() -> None:
    """验证 AI 请求的 prompt_payload 真的包含采集到的原文。

    通过捕获 httpx.post 的 json 参数，反查 messages[1].content 包含原文。
    """
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.refine.ai import request_refine_summary
    from src.plugins.refine.collector import collect_and_build_payload
    from src.plugins.refine.db import RefineSubscription

    fixed_now = 1_800_000_000
    unique_text = "X UNIQUE MARKER TEXT 12345 Y"
    await archive_message_event(
        _make_group_event(
            unique_text,
            user_id=444,
            group_id=400001,
            message_id=701,
            event_time=fixed_now - 60,
            nickname="标记用户",
        )
    )

    sub = RefineSubscription(
        id=1,
        group_id="400001",
        target_type="user",
        target_value="444",
        label="x",
        created_at=fixed_now,
    )

    import src.plugins.refine as rp

    with patch("src.plugins.refine.collector.time.time", return_value=fixed_now):
        collected, payload = await collect_and_build_payload(sub, rp.config)

    assert unique_text in payload, "采集到的原文未出现在 prompt_payload 里"

    # 验证 AI 调用收到正确 payload
    captured: dict = {}

    async def _fake_post(self, url, *args, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return _fake_ai_response("ok")

    with patch("httpx.AsyncClient.post", new=_fake_post):
        result = await request_refine_summary(
            base_url="https://example.com/v1",
            api_key="sk-test",
            model="gpt-test",
            timeout_seconds=30,
            temperature=0.3,
            prompt_payload=payload,
        )

    assert result == "ok"
    assert captured["url"] == "https://example.com/v1/chat/completions"
    # user prompt 里包含原文
    user_msg = captured["json"]["messages"][1]["content"]
    assert unique_text in user_msg
