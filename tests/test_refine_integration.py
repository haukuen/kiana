"""炼化插件集成 / mock-bot 测试（v2 — 懒触发实时提炼）。

与 ``test_refine.py``（单元测试）的差异:
- 不 mock ``fetch_collection_members`` / ``fetch_group_messages_by_time_range``，
  走真实的 un_nickname + message_archive DB 链路。
- 用本地 httpx mock server（或 ``patch`` httpx 顶层 AsyncClient.post）模拟 AI
  端点，但完整跑 collect → prompt → response → 落库 链路。
- 用 ``nonebug`` 完整跑事件 → matcher → handler → DB → 回包 全链路。

不连接真实 OneBot 协议端、不调真实 OpenAI API。

v2 — 不再注册任何 cron job；驱动 ``on_startup`` 钩子只 ensure_schema + 校验 AI 配置。
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


def _fake_dt():
    """返回一个 strftime 始终输出 'T' 的假 datetime 实例。"""
    return type("FakeDT", (), {"strftime": lambda self, fmt: "T"})()


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
        refine_result_fresh_seconds=86400,
        refine_query_cooldown_seconds=60,
        refine_lookback_hours=24,
        refine_max_messages_per_target=200,
        refine_max_prompt_chars=12000,
        refine_min_messages_to_refine=2,
    )


@pytest.fixture(autouse=True)
def _reset_refine_cooldown() -> None:
    """每用例前清空 commands.cooldown_dict。"""
    from src.plugins.refine import commands

    commands.cooldown_dict.clear()


# ═══════════════════════════════════════════════════════════════
# 1. un_nickname 集成：collection 订阅 → 真实 fetch_collection_members
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


# ═══════════════════════════════════════════════════════════════
# 2. message_archive event_preprocessor 集成
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_message_archive_preprocessor_feeds_refine_pipeline(app: App) -> None:
    """用 event_preprocessor 走归档（不直接调 archive_message_event），
    再触发炼化采集，验证完整 preprocess → archive → collect 链路。
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
    with patch("time.time", return_value=float(fixed_now)):
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
# 3. AI 调用真实 payload 验证（不 mock collect，只 mock httpx）
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

    with patch("time.time", return_value=float(fixed_now)):
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


# ═══════════════════════════════════════════════════════════════
# 4. 懒触发全链路：集合订阅 → 灌发言 → 炼化 命令（首次重炼）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_lazy_refine_full_pipeline(app: App) -> None:
    """端到端全链路（collection 订阅）：
    创建集合 → 灌发言 → 订阅 → 炼化 命令（首次重炼）→ 验证落库。

    覆盖 commands + db + collector + ai + runner + format 全部模块。
    """
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.refine import commands
    from src.plugins.refine.db import get_result, get_subscription_by_label
    from src.plugins.un_nickname.db import add_collection_members

    fixed_now = 1_800_000_000

    # 1. 先建集合（2 个成员）
    await add_collection_members("200001", "核心组", ["111", "222"])

    # 2. 灌发言（>= refine_min_messages_to_refine=2）
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
                "发送 `炼化 核心` 查看结果（首次查询会触发提炼）"
            ),
            result={"message_id": 2000},
        )

    sub = await get_subscription_by_label("200001", "核心")
    assert sub is not None
    assert sub.target_type == "collection"
    assert sub.target_value == "核心组"

    # 4. 炼化命令（首次，触发实时提炼），patch AI 返回 + 时间稳定
    fake_dt = _fake_dt()
    lazy_event = _make_group_event(
        "炼化 核心", message_id=201, event_time=fixed_now
    )
    # mute cache 在 first matcher 后是否被填充依赖 nonebug 实现细节，每 matcher
    # 前手动清缓存 + 声明 mute API，保证测试稳定
    from src import plugins as global_plugins

    global_plugins._mute_cache.clear()
    with (
        patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(return_value=_fake_ai_response("集合总结内容")),
        ),
        patch.object(commands, "datetime") as mock_dt,
        patch("time.time", return_value=float(fixed_now)),
    ):
        mock_dt.fromtimestamp.return_value = fake_dt
        async with app.test_matcher(commands.refine_lazy) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            _expect_bot_not_muted(ctx)
            ctx.receive_event(bot, lazy_event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                lazy_event,
                "⏳ 正在为 [核心] 提炼，请稍候...",
                result={"message_id": 2001},
            )
            ctx.should_call_send(
                lazy_event,
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
    result = await get_result(sub.id)
    assert result is not None
    assert result.summary == "集合总结内容"
    assert result.message_count == 2


# ═══════════════════════════════════════════════════════════════
# 5. 强制炼化覆盖新鲜缓存
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_force_refine_overrides_fresh_cache(app: App) -> None:
    """新鲜缓存下，`强制炼化` 应跳过新鲜检查并覆盖缓存。"""
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.refine import commands
    from src.plugins.refine.db import (
        add_subscription,
        get_result,
        save_result,
    )

    fixed_now = 1_800_000_000

    sub = await add_subscription(
        group_id="200001", target_type="user", target_value="555", label="目标"
    )
    assert sub is not None

    # 1. 灌新鲜缓存（created_at 在 fresh_seconds 内）
    await save_result(
        subscription_id=sub.id,
        period_start=fixed_now - 60,
        period_end=fixed_now,
        summary="新鲜但将被强制覆盖",
        message_count=2,
        model_name="gpt-test",
    )

    # 2. 灌发言供采集
    for i in range(3):
        await archive_message_event(
            _make_group_event(
                f"目标发言内容{i}",
                user_id=555,
                group_id=200001,
                message_id=600 + i,
                event_time=fixed_now - 600 + i * 60,
                nickname="目标",
            )
        )

    # 3. 强制炼化命令
    fake_dt = _fake_dt()
    force_event = _make_group_event(
        "强制炼化 目标", message_id=700, event_time=fixed_now
    )
    with (
        patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(return_value=_fake_ai_response("强制重炼的新总结")),
        ),
        patch.object(commands, "datetime") as mock_dt,
        patch("time.time", return_value=float(fixed_now)),
    ):
        mock_dt.fromtimestamp.return_value = fake_dt
        async with app.test_matcher(commands.refine_force) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            _expect_bot_not_muted(ctx)
            ctx.receive_event(bot, force_event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                force_event,
                "⏳ 正在为 [目标] 强制提炼，请稍候...",
                result={"message_id": 7001},
            )
            ctx.should_call_send(
                force_event,
                (
                    "🧪 炼化结果：[目标]\n"
                    "目标：用户=555\n"
                    "采样窗口：T ~ T\n"
                    "采样消息：3 条\n"
                    "模型：gpt-test\n"
                    "\n"
                    "强制重炼的新总结"
                ),
                result={"message_id": 7002},
            )

    # 4. 验证落库被覆盖
    result = await get_result(sub.id)
    assert result is not None
    assert result.summary == "强制重炼的新总结"
    assert result.message_count == 3


# ═══════════════════════════════════════════════════════════════
# 6. bug#1 回归：命令字后必须有空格（force_whitespace）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_lazy_ignores_glued_text_no_space(app: App) -> None:
    """bug#1 回归：`炼化XXX`（无空格粘连）不触发炼化命令。

    复现：用户发 `炼化这个功能怎么用`，历史 bug 会把它解析为
    `炼化` 命令 + arg="这个功能怎么用"，handler 把 arg 当 label
    查订阅并回复「未找到标签为「这个功能怎么用」的订阅」。

    修复：commands.py 给所有 on_command 加 force_whitespace=True，
    强制命令字与参数间必须有空格才触发。
    """
    from src.plugins.refine import commands

    glued_event = _make_group_event(
        "炼化这个功能怎么用", message_id=800
    )
    async with app.test_matcher(commands.refine_lazy) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        ctx.receive_event(bot, glued_event)
        ctx.should_not_pass_rule()


@pytest.mark.asyncio
async def test_force_ignores_glued_text_no_space(app: App) -> None:
    """bug#1 回归：`强制炼化XXX`（无空格粘连）不触发强制炼化命令。"""
    from src.plugins.refine import commands

    glued_event = _make_group_event(
        "强制炼化这个功能怎么用", message_id=801
    )
    async with app.test_matcher(commands.refine_force) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        ctx.receive_event(bot, glued_event)
        ctx.should_not_pass_rule()


@pytest.mark.asyncio
async def test_lazy_works_with_space(app: App) -> None:
    """对照（回归保护）：`炼化 张三`（有空格）仍正常触发。

    用一个不存在的 label，验证命令字 + 空格 + 参数 路径走通，
    handler 正常进入并回复「未找到标签」——说明 force_whitespace
    没有误伤正常的带空格调用。
    """
    from src.plugins.refine import commands

    spaced_event = _make_group_event("炼化 张三", message_id=802)
    async with app.test_matcher(commands.refine_lazy) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, spaced_event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            spaced_event,
            "未找到标签为「张三」的订阅",
            result={"message_id": 8002},
        )


@pytest.mark.asyncio
async def test_subscribe_ignores_glued_text_no_space(app: App) -> None:
    """bug#1 回归：`炼化订阅XXX`（无空格粘连）不触发订阅命令。

    覆盖 force_whitespace=True 对 refine_subscribe 的保护。
    """
    from src.plugins.refine import commands

    glued_event = _make_group_event("炼化订阅xxx", message_id=803)
    async with app.test_matcher(commands.refine_subscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        ctx.receive_event(bot, glued_event)
        ctx.should_not_pass_rule()


@pytest.mark.asyncio
async def test_list_ignores_glued_text_no_space(app: App) -> None:
    """bug#1 回归：`炼化订阅列表XXX`（无空格粘连）不触发列表命令。

    覆盖 force_whitespace=True 对 refine_list 的保护。
    注意：refine_list 是无参数命令,粘连后缀文本应被拦截。
    """
    from src.plugins.refine import commands

    glued_event = _make_group_event("炼化订阅列表xxx", message_id=804)
    async with app.test_matcher(commands.refine_list) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        ctx.receive_event(bot, glued_event)
        ctx.should_not_pass_rule()


@pytest.mark.asyncio
async def test_unsubscribe_ignores_glued_text_no_space(app: App) -> None:
    """bug#1 回归：`炼化取消订阅XXX`（无空格粘连）不触发取消订阅命令。

    覆盖 force_whitespace=True 对 refine_unsubscribe 的保护。
    """
    from src.plugins.refine import commands

    glued_event = _make_group_event("炼化取消订阅xxx", message_id=805)
    async with app.test_matcher(commands.refine_unsubscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        ctx.receive_event(bot, glued_event)
        ctx.should_not_pass_rule()


@pytest.mark.asyncio
async def test_force_glued_with_extra_suffix_ignored(app: App) -> None:
    """bug#1 回归补强：`强制炼化xxx`（不同粘连文本）也不触发强制炼化命令。

    覆盖 force_whitespace=True 对 refine_force 在不同粘连文本下的保护
    (test_force_ignores_glued_text_no_space 已覆盖中文粘连,本用例补英文/符号粘连)。
    """
    from src.plugins.refine import commands

    glued_event = _make_group_event("强制炼化_test_suffix", message_id=806)
    async with app.test_matcher(commands.refine_force) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        ctx.receive_event(bot, glued_event)
        ctx.should_not_pass_rule()


@pytest.mark.asyncio
async def test_help_ignores_glued_text_no_space(app: App) -> None:
    """bug#1 回归：`炼化帮助XXX`（无空格粘连）不触发帮助命令。

    覆盖 force_whitespace=True 对 refine_help 的保护。
    注意：refine_help 是无参数命令,粘连后缀文本应被拦截。
    """
    from src.plugins.refine import commands

    glued_event = _make_group_event("炼化帮助xxx", message_id=807)
    async with app.test_matcher(commands.refine_help) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        ctx.receive_event(bot, glued_event)
        ctx.should_not_pass_rule()


# ═══════════════════════════════════════════════════════════════
# 7. bug#2 回归：集合炼化 per-member 独立配额
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_collect_distributes_quota_per_member_not_shared() -> None:
    """bug#2 回归：集合炼化的 max_messages 应是每成员独立配额，而非集合共享。

    场景：集合{A, B, C}，A 早期发 8 条，B/C 后期各发 8 条，max_messages=8。
    旧实现（共享预算）：A 8 条吃光预算 → B/C 0 条。
    新实现（per-member）：A 8 + B 8 + C 8 = 24 条，三人全覆盖。
    """
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.refine.collector import collect_messages_for_subscription
    from src.plugins.refine.db import RefineSubscription
    from src.plugins.un_nickname.db import add_collection_members

    # 构造集合 3 成员
    await add_collection_members("200001", "test_bug2_collection", ["111", "222", "333"])

    # A 早期发 8 条（event_time 早），B/C 后期发 8 条（event_time 晚）
    base_ts = 1_800_000_000
    for i in range(8):
        await archive_message_event(
            _make_group_event(
                f"A的早期发言{i}",
                user_id=111,
                group_id=200001,
                message_id=1000 + i,
                event_time=base_ts - 1200 + i,
                nickname="成员A",
            )
        )
    for i in range(8):
        await archive_message_event(
            _make_group_event(
                f"B的后期发言{i}",
                user_id=222,
                group_id=200001,
                message_id=2000 + i,
                event_time=base_ts - 100 + i,
                nickname="成员B",
            )
        )
    for i in range(8):
        await archive_message_event(
            _make_group_event(
                f"C的后期发言{i}",
                user_id=333,
                group_id=200001,
                message_id=3000 + i,
                event_time=base_ts - 50 + i,
                nickname="成员C",
            )
        )

    sub = RefineSubscription(
        id=999,
        group_id="200001",
        target_type="collection",
        target_value="test_bug2_collection",
        label="test",
        created_at=base_ts,
    )

    with patch("time.time", return_value=float(base_ts + 3600)):
        collected = await collect_messages_for_subscription(
            sub, lookback_hours=24, max_messages=8, max_prompt_chars=12000
        )

    # 关键断言：三人各 8 条，共 24 条
    assert len(collected.messages) == 24
    # 各成员贡献均衡
    a_count = sum(1 for _, name, _ in collected.messages if name == "成员A")
    b_count = sum(1 for _, name, _ in collected.messages if name == "成员B")
    c_count = sum(1 for _, name, _ in collected.messages if name == "成员C")
    assert a_count == 8
    assert b_count == 8
    assert c_count == 8


@pytest.mark.asyncio
async def test_collect_per_member_quota_with_small_max() -> None:
    """bug#2 回归对照：max_messages=2 时，每个成员各采 2 条（而非集合共享只采 2 条）。

    场景：集合 2 成员，各发 5 条，max_messages=2。
    旧实现（共享预算）：仅采到前 2 条（A 全部），B 0 条。
    新实现（per-member）：A 2 + B 2 = 4 条，两人都覆盖。
    """
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.refine.collector import collect_messages_for_subscription
    from src.plugins.refine.db import RefineSubscription
    from src.plugins.un_nickname.db import add_collection_members

    await add_collection_members("200001", "test_bug2_small_collection", ["444", "555"])

    base_ts = 1_800_000_000
    # A 早期发 5 条，B 后期发 5 条
    for i in range(5):
        await archive_message_event(
            _make_group_event(
                f"小A发言{i}",
                user_id=444,
                group_id=200001,
                message_id=4000 + i,
                event_time=base_ts - 1200 + i,
                nickname="成员D",
            )
        )
    for i in range(5):
        await archive_message_event(
            _make_group_event(
                f"小B发言{i}",
                user_id=555,
                group_id=200001,
                message_id=5000 + i,
                event_time=base_ts - 100 + i,
                nickname="成员E",
            )
        )

    sub = RefineSubscription(
        id=998,
        group_id="200001",
        target_type="collection",
        target_value="test_bug2_small_collection",
        label="test_small",
        created_at=base_ts,
    )

    with patch("time.time", return_value=float(base_ts + 3600)):
        collected = await collect_messages_for_subscription(
            sub, lookback_hours=24, max_messages=2, max_prompt_chars=12000
        )

    # 期望：每人 2 条，共 4 条
    assert len(collected.messages) == 4
    d_count = sum(1 for _, name, _ in collected.messages if name == "成员D")
    e_count = sum(1 for _, name, _ in collected.messages if name == "成员E")
    assert d_count == 2
    assert e_count == 2


# ═══════════════════════════════════════════════════════════════
# 8. commands.py handler body 分支覆盖
# ═══════════════════════════════════════════════════════════════
# 这些测试针对 commands.py 里 handler 的具体分支(原 force_whitespace 只覆盖
# rule 拦截,handler 内部分支仍未覆盖,所以 commands.py 卡在 75%)。


@pytest.mark.asyncio
async def test_help_handler_replies_help_text(app: App) -> None:
    """refine_help handler body: `炼化帮助` 返回完整帮助文案。"""
    from src.plugins.refine import commands

    expected = "\n".join([
        "🧪 炼化插件帮助",
        "",
        "订阅某个用户或某集合的发言，按需用 AI 生成简要总结；",
        "结果与订阅一一对应，新结果自动覆盖旧结果。",
        "",
        "命令：",
        "  炼化订阅 <标签> user:<qq>",
        "  炼化订阅 <标签> collection:<名>",
        "  炼化订阅 <标签> 集合 <名>",
        "  炼化订阅 <标签> @某人",
        "  炼化订阅列表",
        "  炼化取消订阅 <标签>",
        "  炼化 <标签>          # 缓存新鲜直接返回，过期才重炼",
        "  强制炼化 <标签>      # 跳过新鲜检查与冷却，强制重炼",
    ])

    event = _make_group_event("炼化帮助", message_id=900)
    async with app.test_matcher(commands.refine_help) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, expected, result={"message_id": 9001})


@pytest.mark.asyncio
async def test_list_handler_no_subscriptions(app: App) -> None:
    """refine_list handler body: 无订阅时返回空提示。"""
    from src.plugins.refine import commands

    event = _make_group_event("炼化订阅列表", message_id=901)
    async with app.test_matcher(commands.refine_list) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "本群暂无炼化订阅。发送 `炼化订阅 <标签> @某人` 添加",
            result={"message_id": 9010},
        )


@pytest.mark.asyncio
async def test_list_handler_with_subscription(app: App) -> None:
    """refine_list handler body: 有订阅时返回格式化列表。"""
    from datetime import datetime as _dt

    from src.plugins.refine import commands
    from src.plugins.refine.db import add_subscription
    from src.storage import get_db

    sub = await add_subscription(
        group_id="200001", target_type="user", target_value="555", label="目标"
    )
    assert sub is not None

    # 把 created_at 改成固定值,使 _format_subscription_line 的输出稳定
    fixed_now = 1_700_000_000
    await get_db().execute(
        "UPDATE refine_subscription SET created_at = ? WHERE id = ?",
        (fixed_now, sub.id),
    )

    expected_line = (
        f"  • [目标] 用户=555  "
        f"(创建于 {_dt.fromtimestamp(fixed_now).strftime('%Y-%m-%d %H:%M')})"
    )
    expected_msg = (
        f"📌 本群共 1 个炼化订阅:\n"
        f"{expected_line}\n"
        f"\n使用 `炼化 <标签>` 查看或触发提炼"
    )

    event = _make_group_event("炼化订阅列表", message_id=902)
    async with app.test_matcher(commands.refine_list) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, expected_msg, result={"message_id": 9020})


@pytest.mark.asyncio
async def test_unsubscribe_handler_no_label(app: App) -> None:
    """refine_unsubscribe handler body: 无标签参数 → 用法提示。"""
    from src.plugins.refine import commands

    # 空参数
    event = _make_group_event("炼化取消订阅", message_id=903)
    async with app.test_matcher(commands.refine_unsubscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "用法：炼化取消订阅 <标签>", result={"message_id": 9030})


@pytest.mark.asyncio
async def test_unsubscribe_handler_label_not_found(app: App) -> None:
    """refine_unsubscribe handler body: 标签不存在 → 未找到提示。"""
    from src.plugins.refine import commands

    event = _make_group_event("炼化取消订阅 不存在的标签", message_id=904)
    async with app.test_matcher(commands.refine_unsubscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "未找到标签为「不存在的标签」的订阅", result={"message_id": 9040})


@pytest.mark.asyncio
async def test_unsubscribe_handler_success(app: App) -> None:
    """refine_unsubscribe handler body: 成功取消订阅。"""
    from src.plugins.refine import commands
    from src.plugins.refine.db import add_subscription

    sub = await add_subscription(
        group_id="200001", target_type="user", target_value="555", label="要删的"
    )
    assert sub is not None

    event = _make_group_event("炼化取消订阅 要删的", message_id=905)
    async with app.test_matcher(commands.refine_unsubscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event, "✅ 已取消订阅 [要删的]，历史结果一并删除", result={"message_id": 9050}
        )


@pytest.mark.asyncio
async def test_subscribe_handler_no_args(app: App) -> None:
    """refine_subscribe handler body: 仅 `炼化订阅` 无参数 → 用法提示。"""
    from src.plugins.refine import commands

    event = _make_group_event("炼化订阅", message_id=906)
    async with app.test_matcher(commands.refine_subscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "用法：炼化订阅 <标签> <user:qq | collection:名 | 集合 名 | @某人>",
            result={"message_id": 9060},
        )


@pytest.mark.asyncio
async def test_subscribe_handler_unresolvable_target(app: App) -> None:
    """refine_subscribe handler body: 有标签但目标前缀不识别 → 无法识别提示。

    parts 长度 >= 2 才会走 resolve_target_type_and_value;`foo:bar` 的 prefix
    不是 user/collection/集合,返回 (None, None)。
    """
    from src.plugins.refine import commands

    event = _make_group_event("炼化订阅 标签 foo:bar", message_id=907)
    async with app.test_matcher(commands.refine_subscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            (
                "无法识别订阅目标。支持的写法：\n"
                "  user:<qq>\n"
                "  collection:<名>\n"
                "  集合 <名>\n"
                "  或直接 @某人"
            ),
            result={"message_id": 9070},
        )


@pytest.mark.asyncio
async def test_subscribe_handler_dup_target(app: App) -> None:
    """refine_subscribe handler body: 目标已被订阅 → 冲突提示。"""
    from src.plugins.refine import commands
    from src.plugins.refine.db import add_subscription

    existing = await add_subscription(
        group_id="200001", target_type="user", target_value="555", label="已有"
    )
    assert existing is not None

    event = _make_group_event("炼化订阅 新标签 user:555", message_id=908)
    async with app.test_matcher(commands.refine_subscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "该目标已被订阅，标签为「已有」",
            result={"message_id": 9080},
        )


@pytest.mark.asyncio
async def test_subscribe_handler_dup_label(app: App) -> None:
    """refine_subscribe handler body: 标签已被使用 → 冲突提示。"""
    from src.plugins.refine import commands
    from src.plugins.refine.db import add_subscription

    existing = await add_subscription(
        group_id="200001", target_type="user", target_value="555", label="重复标签"
    )
    assert existing is not None

    event = _make_group_event("炼化订阅 重复标签 user:666", message_id=909)
    async with app.test_matcher(commands.refine_subscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event, "标签「重复标签」已被使用，请换一个", result={"message_id": 9090}
        )


@pytest.mark.asyncio
async def test_subscribe_handler_collection_not_exist(app: App) -> None:
    """refine_subscribe handler body: 集合不存在 → 提示创建。"""
    from src.plugins.refine import commands

    event = _make_group_event("炼化订阅 标签 collection:不存在的集合", message_id=910)
    async with app.test_matcher(commands.refine_subscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            (
                "集合「不存在的集合」不存在或无成员。"
                "请先用 un_nickname 插件的 `集合 <名> @人` 命令创建。"
            ),
            result={"message_id": 9100},
        )


@pytest.mark.asyncio
async def test_lazy_handler_no_label(app: App) -> None:
    """refine_lazy handler body: 仅 `炼化` 无参数 → 用法提示。"""
    from src.plugins.refine import commands

    event = _make_group_event("炼化", message_id=911)
    async with app.test_matcher(commands.refine_lazy) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "用法：炼化 <标签>", result={"message_id": 9110})


@pytest.mark.asyncio
async def test_lazy_handler_config_missing(app: App) -> None:
    """refine_lazy handler body: AI 配置缺失 → 配置错误提示。

    用一个存在的订阅走到 validate_ai_config,然后 patch config 缺 api_key。
    """
    from src.plugins.refine import commands
    from src.plugins.refine.db import add_subscription

    sub = await add_subscription(
        group_id="200001", target_type="user", target_value="555", label="配置测"
    )
    assert sub is not None

    # 临时把 api_key 清空(本文件 autouse fixture 已配好,这里覆盖)
    import src.plugins.refine as rp
    saved = rp.config.refine_ai_api_key
    rp.config.refine_ai_api_key = ""
    try:
        event = _make_group_event("炼化 配置测", message_id=912)
        async with app.test_matcher(commands.refine_lazy) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            _expect_bot_not_muted(ctx)
            ctx.receive_event(bot, event)
            ctx.should_pass_rule()
            # validate_ai_config 的具体报错文案取决于 RefineConfigError 信息,
            # 这里只校验前缀(完整文案可能与具体配置项有关)
            ctx.should_call_send(
                event,
                "❌ AI 配置缺失：refine_ai_api_key 未配置",
                result={"message_id": 9120},
            )
    finally:
        rp.config.refine_ai_api_key = saved


@pytest.mark.asyncio
async def test_lazy_handler_label_not_found(app: App) -> None:
    """refine_lazy handler body: 标签不存在 → 未找到提示。"""
    from src.plugins.refine import commands

    event = _make_group_event("炼化 不存在的标签", message_id=913)
    async with app.test_matcher(commands.refine_lazy) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "未找到标签为「不存在的标签」的订阅", result={"message_id": 9130})


@pytest.mark.asyncio
async def test_force_handler_no_label(app: App) -> None:
    """refine_force handler body: 仅 `强制炼化` 无参数 → 用法提示。"""
    from src.plugins.refine import commands

    event = _make_group_event("强制炼化", message_id=914)
    async with app.test_matcher(commands.refine_force) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "用法：强制炼化 <标签>", result={"message_id": 9140})


@pytest.mark.asyncio
async def test_force_handler_label_not_found(app: App) -> None:
    """refine_force handler body: 标签不存在 → 未找到提示。"""
    from src.plugins.refine import commands

    event = _make_group_event("强制炼化 不存在的标签", message_id=915)
    async with app.test_matcher(commands.refine_force) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "未找到标签为「不存在的标签」的订阅", result={"message_id": 9150})
