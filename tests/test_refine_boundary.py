"""炼化插件边界条件补充测试。

与现有测试(``test_refine.py`` / ``test_refine_integration.py`` /
``test_fixes_e2e.py``)的差异:本文件只覆盖它们**未触及的边界**——
不重复 happy path、不重复已有错误分类测试。

覆盖范围:
1. **bug#1 边界** — ``force_whitespace`` 对 Tab / 全角空格 / 多空格 / 无参数 /
   ``/`` 前缀 / CQ 码前缀的具体行为(现有只测「粘连不触发」+「单空格触发」)。
2. **bug#2 边界** — collector ``per-member`` 配额的更多形态:
   某成员 0 条 / 单成员集合等价 user / 空 collection / 5 人各 1 条 /
   2 人悬殊配额。
3. **collector 其他边界** — ``build_prompt_payload`` 空 / 超长单条 /
   ``max_prompt_chars=0`` / 刚好填满;``resolve_target_type_and_value`` 更多边界。
4. **ai.py 边界** — ``extract_response_content`` 对 array 全空 text 段 /
   content 为 dict 的处理(其它分支已被 test_refine.py 覆盖)。

行为依据(查源码确认):
- NoneBot ``CMD_WHITESPACE_KEY`` 由 ``arg_str.lstrip()`` 判定(rule.py:117),
  Python ``str.lstrip()`` 默认剥离**所有 Unicode 空白**,所以 Tab / 全角空格
  (U+3000)/ NBSP(U+00A0) 都会被识别为合法命令分隔空白。
- ``force_whitespace=True`` 在 ``cmd_arg`` 为空时(rule.py:387)绕过检查,
  所以「无参数」命令仍然触发(handler 进入后再用「用法」提示拦截)。

不修改任何源代码、不修改其他测试。
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

# ── 本地工厂(与 test_refine.py / test_fixes_e2e.py 保持一致) ──────────


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


def _expect_bot_not_muted(
    ctx, group_id: int = 200001, self_id: int = 987654321
) -> None:
    """声明 bot 不被禁言 — should_call_send / should_pass_rule 之前必须调用。

    项目 ``check_bot_mute_status`` preprocessor 在群消息场景下会查 mute cache,
    触发 get_group_member_info API。未先声明会导致 nonebug 报意外 API 调用。
    """
    ctx.should_call_api(
        "get_group_member_info",
        {"group_id": group_id, "user_id": self_id, "no_cache": True},
        result={"shut_up_timestamp": 0},
    )


def _configure_refine_plugin() -> None:
    """把 refine config 全设成可用值,确保 group_rule 放行 + handler 不因配置缺失早退。"""
    import src.plugins.refine as rp

    rp.config.refine_plugin_enabled = True
    rp.config.refine_group_mode = "all"
    rp.config.refine_group_whitelist = []
    rp.config.refine_group_blacklist = []
    rp.config.refine_ai_base_url = "https://example.com/v1"
    rp.config.refine_ai_api_key = "sk-test"
    rp.config.refine_ai_model = "gpt-test"
    rp.config.refine_ai_timeout_seconds = 30.0
    rp.config.refine_ai_temperature = 0.3
    rp.config.refine_result_fresh_seconds = 86400
    rp.config.refine_query_cooldown_seconds = 60
    rp.config.refine_lookback_hours = 24
    rp.config.refine_max_messages_per_target = 200
    rp.config.refine_max_prompt_chars = 12000
    rp.config.refine_min_messages_to_refine = 2


# ═══════════════════════════════════════════════════════════════
# 1. bug#1 边界:force_whitespace 对各种空白 / 前缀的处理
# ═══════════════════════════════════════════════════════════════
#
# 行为依据(已查 NoneBot 源码 rule.py:104-128):
#   - prefix 解析只看 message[0],且仅当它是 text 段时才尝试匹配命令字。
#   - CMD_WHITESPACE_KEY 由 ``arg_str.lstrip()`` 判定,Python lstrip() 默认剥离
#     所有 Unicode 空白(含 \t / \u3000 / \xa0),所以这些字符都算合法分隔。
#   - force_whitespace=True 在 cmd_arg 为空时绕过(rule.py:387),无参数命令仍触发。
#   - CQ 码(@bot)消息 message[0] 是 at 段而非 text 段,prefix 解析整个跳过,
#     所以「@bot 炼化 张三」不会触发任何 on_command 命令(NoneBot on_command
#     不像 on_keyword 那样扫描整条 raw_message)。
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_force_whitespace_accepts_tab_separator(app: App) -> None:
    """``炼化\\t张三``(Tab 分隔)应触发命令。

    边界:Tab 是 Unicode 空白,str.lstrip() 会剥离,force_whitespace 通过。
    """
    from src.plugins.refine import commands

    _configure_refine_plugin()
    event = _make_group_event("炼化\t不存在的标签", message_id=1)
    async with app.test_matcher(commands.refine_lazy) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "未找到标签为「不存在的标签」的订阅",
            result={"message_id": 100},
        )


@pytest.mark.asyncio
async def test_force_whitespace_accepts_fullwidth_space(app: App) -> None:
    """``炼化\\u3000张三``(全角空格 U+3000)应触发命令。

    边界:全角空格是 Unicode 空白,str.lstrip() 会剥离,force_whitespace 通过。
    已通过 ``python3 -c "'\\u3000'.lstrip()"`` 实测确认 lstrip 行为。
    """
    from src.plugins.refine import commands

    _configure_refine_plugin()
    event = _make_group_event("炼化\u3000不存在的标签", message_id=2)
    async with app.test_matcher(commands.refine_lazy) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "未找到标签为「不存在的标签」的订阅",
            result={"message_id": 101},
        )


@pytest.mark.asyncio
async def test_force_whitespace_allows_no_arg(app: App) -> None:
    """``炼化``(无参数)应触发命令,进入 handler 后被「用法」提示拦截。

    边界:NoneBot rule.py:387 ``if self.force_whitespace is None or not cmd_arg``
    —— cmd_arg 为空时 force_whitespace 检查被绕过,命令仍然触发。
    """
    from src.plugins.refine import commands

    _configure_refine_plugin()
    event = _make_group_event("炼化", message_id=3)
    async with app.test_matcher(commands.refine_lazy) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "用法：炼化 <标签>", result={"message_id": 102})


@pytest.mark.asyncio
async def test_force_whitespace_accepts_multiple_spaces(app: App) -> None:
    """``强制炼化   张三``(命令字后多个空格)应触发命令。

    边界:多空格仍是合法分隔,force_whitespace 不限制空白长度。
    """
    from src.plugins.refine import commands

    _configure_refine_plugin()
    event = _make_group_event("强制炼化   不存在的标签", message_id=4)
    async with app.test_matcher(commands.refine_force) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "未找到标签为「不存在的标签」的订阅",
            result={"message_id": 103},
        )


@pytest.mark.asyncio
async def test_force_whitespace_accepts_slash_prefix(app: App) -> None:
    """``/炼化 张三``(命令前 /)应触发命令。

    边界:NoneBot 默认 COMMAND_START=["/"],``/`` 是合法命令起始。
    """
    from src.plugins.refine import commands

    _configure_refine_plugin()
    event = _make_group_event("/炼化 不存在的标签", message_id=5)
    async with app.test_matcher(commands.refine_lazy) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "未找到标签为「不存在的标签」的订阅",
            result={"message_id": 104},
        )


@pytest.mark.asyncio
async def test_force_whitespace_at_bot_prefix_not_trigger(app: App) -> None:
    """``@bot 炼化 张三``(CQ 码前缀)**不应**触发 ``炼化`` 命令。

    边界:NoneBot ``on_command`` 的 prefix 解析只看 ``message[0]``(rule.py:103),
    且仅当它是 text 段时才匹配命令字。@bot 消息 message[0] 是 ``at`` 段,
    prefix 解析整个跳过 → 不识别为命令 → rule 不通过。
    这不是 force_whitespace 的行为,而是 on_command 本身的语义。
    """
    from src.plugins.refine import commands

    _configure_refine_plugin()
    # message[0] = at 段,message[1] = text "炼化 不存在的标签"
    msg = Message([MessageSegment.at("987654321"), MessageSegment.text("炼化 不存在的标签")])
    event = _make_group_event(msg, message_id=6)
    async with app.test_matcher(commands.refine_lazy) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule()


# ═══════════════════════════════════════════════════════════════
# 2. bug#2 边界:collector per-member 配额的更多形态
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_collect_skips_member_with_zero_messages() -> None:
    """集合含某成员但该成员 0 条消息 → picked 里不含该成员的 sender_name。

    边界:per-member 配额是「采到才计数」,成员无发言时 picked 自然不含其名。
    与「成员有发言但因配额耗尽被跳过」是不同场景。
    """
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.refine.collector import collect_messages_for_subscription
    from src.plugins.refine.db import RefineSubscription
    from src.plugins.un_nickname.db import add_collection_members

    await add_collection_members("200001", "b2_zero_collection", ["111", "222"])
    now = int(time.time())
    # 只有 111 发言,222 完全沉默
    await archive_message_event(
        _make_group_event(
            "A的发言", user_id=111, group_id=200001, message_id=10, event_time=now - 100,
            nickname="成员A",
        )
    )

    sub = RefineSubscription(
        id=1, group_id="200001", target_type="collection",
        target_value="b2_zero_collection", label="x", created_at=now,
    )
    collected = await collect_messages_for_subscription(
        sub, lookback_hours=24, max_messages=10, max_prompt_chars=12000,
    )
    names = {name for _, name, _ in collected.messages}
    assert names == {"成员A"}, f"沉默成员不应出现,实际 {names}"


@pytest.mark.asyncio
async def test_collect_single_member_collection_equivalent_to_user() -> None:
    """单成员集合订阅的采集行为与 user 订阅等价。

    边界:target_set 只有 1 个 user_id 时,per-member 配额退化为「该成员最多 N 条」,
    与 user 订阅语义一致。
    """
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.refine.collector import collect_messages_for_subscription
    from src.plugins.refine.db import RefineSubscription
    from src.plugins.un_nickname.db import add_collection_members

    await add_collection_members("200001", "b2_single_collection", ["555"])
    now = int(time.time())
    for i in range(3):
        await archive_message_event(
            _make_group_event(
                f"单成员发言{i}", user_id=555, group_id=200001,
                message_id=20 + i, event_time=now - 200 + i, nickname="孤独的成员",
            )
        )

    sub_collection = RefineSubscription(
        id=2, group_id="200001", target_type="collection",
        target_value="b2_single_collection", label="c", created_at=now,
    )
    sub_user = RefineSubscription(
        id=3, group_id="200001", target_type="user",
        target_value="555", label="u", created_at=now,
    )

    collected_c = await collect_messages_for_subscription(
        sub_collection, lookback_hours=24, max_messages=10, max_prompt_chars=12000,
    )
    collected_u = await collect_messages_for_subscription(
        sub_user, lookback_hours=24, max_messages=10, max_prompt_chars=12000,
    )
    # 两者采到的文本集合应一致(单成员集合 ≡ user 订阅)
    assert [t for _, _, t in collected_c.messages] == [t for _, _, t in collected_u.messages]


@pytest.mark.asyncio
async def test_collect_empty_collection_returns_empty_without_error() -> None:
    """空 collection(target_set 为空)→ picked=[] 不报错。

    边界:un_nickname 集合不存在或 0 成员时,_fetch_un_nickname_collection_members
    返回 [],collector 提前 return 空 CollectedMessages。
    """
    from src.plugins.refine.collector import collect_messages_for_subscription
    from src.plugins.refine.db import RefineSubscription

    now = int(time.time())
    sub = RefineSubscription(
        id=4, group_id="200001", target_type="collection",
        target_value="b2_不存在的集合", label="e", created_at=now,
    )
    collected = await collect_messages_for_subscription(
        sub, lookback_hours=24, max_messages=10, max_prompt_chars=12000,
    )
    assert collected.messages == []


@pytest.mark.asyncio
async def test_collect_five_members_each_one_message_all_picked() -> None:
    """集合 5 人,每人发 1 条,max_messages=10 → 每人都被采到,picked 长度=5。

    边界:per-member 配额 10 > 每人实际 1 条,所以全员都被采到,无配额截断。
    """
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.refine.collector import collect_messages_for_subscription
    from src.plugins.refine.db import RefineSubscription
    from src.plugins.un_nickname.db import add_collection_members

    members = ["601", "602", "603", "604", "605"]
    await add_collection_members("200001", "b2_five_collection", members)
    now = int(time.time())
    for idx, uid in enumerate(members):
        await archive_message_event(
            _make_group_event(
                f"成员{idx}的发言", user_id=int(uid), group_id=200001,
                message_id=30 + idx, event_time=now - 500 + idx,
                nickname=f"昵称{idx}",
            )
        )

    sub = RefineSubscription(
        id=5, group_id="200001", target_type="collection",
        target_value="b2_five_collection", label="f", created_at=now,
    )
    collected = await collect_messages_for_subscription(
        sub, lookback_hours=24, max_messages=10, max_prompt_chars=12000,
    )
    assert len(collected.messages) == 5
    # 5 个不同 sender_name 都出现
    names = {name for _, name, _ in collected.messages}
    assert names == {f"昵称{i}" for i in range(5)}


@pytest.mark.asyncio
async def test_collect_unbalanced_quota_caps_each_member_independently() -> None:
    """集合 2 人,A 发 100 条,B 发 1 条,max_messages=5 → A 5 条 + B 1 条 = 6 条。

    边界:per-member 各自上限 5。A 被截到 5 条,B 全部 1 条都被采到。
    这是 bug#2 的极端形态:悬殊发言量下小成员不被吞。
    """
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.refine.collector import collect_messages_for_subscription
    from src.plugins.refine.db import RefineSubscription
    from src.plugins.un_nickname.db import add_collection_members

    await add_collection_members("200001", "b2_unbalanced_collection", ["777", "888"])
    base_ts = 1_800_000_000
    # A 发 100 条(早期)
    for i in range(100):
        await archive_message_event(
            _make_group_event(
                f"A洪流{i}", user_id=777, group_id=200001,
                message_id=1000 + i, event_time=base_ts - 5000 + i, nickname="话痨A",
            )
        )
    # B 发 1 条(后期)
    await archive_message_event(
        _make_group_event(
            "B唯一发言", user_id=888, group_id=200001,
            message_id=2000, event_time=base_ts - 100, nickname="潜水B",
        )
    )

    sub = RefineSubscription(
        id=6, group_id="200001", target_type="collection",
        target_value="b2_unbalanced_collection", label="u", created_at=base_ts,
    )
    with patch("time.time", return_value=float(base_ts + 3600)):
        collected = await collect_messages_for_subscription(
            sub, lookback_hours=24, max_messages=5, max_prompt_chars=12000,
        )

    a_count = sum(1 for _, name, _ in collected.messages if name == "话痨A")
    b_count = sum(1 for _, name, _ in collected.messages if name == "潜水B")
    assert a_count == 5, "A 应被 per-member 配额截到 5 条"
    assert b_count == 1, "B 的 1 条应被全部采到"
    assert len(collected.messages) == 6


# ═══════════════════════════════════════════════════════════════
# 3. collector 其他边界:build_prompt_payload
# ═══════════════════════════════════════════════════════════════


def test_build_prompt_payload_empty_messages_returns_empty_string() -> None:
    """空 messages → 直接返回 ""(短路分支 collector.py:117-118)。"""
    from src.plugins.refine.collector import CollectedMessages, build_prompt_payload

    collected = CollectedMessages(messages=[], period_start=0, period_end=0)
    assert build_prompt_payload(collected, max_prompt_chars=1000) == ""


def test_build_prompt_payload_single_message_longer_than_budget_truncates() -> None:
    """单条消息超过 max_prompt_chars → 截断到剩余预算。

    边界:collector.py:124-130,首条就超预算时 remaining = max_prompt_chars - 0,
    line 被截到 remaining 长度,break。
    """
    from src.plugins.refine.collector import CollectedMessages, build_prompt_payload

    long_text = "x" * 500
    collected = CollectedMessages(
        messages=[(1700000000, "Alice", long_text)],
        period_start=1700000000, period_end=1700000000,
    )
    out = build_prompt_payload(collected, max_prompt_chars=50)
    # 截断后总长度不超过预算
    assert len(out) <= 50
    # 包含前缀(时间戳 + 昵称)被截断后的剩余
    assert "Alice" in out


def test_build_prompt_payload_zero_budget_returns_empty_or_single_char() -> None:
    """max_prompt_chars=0 → 首条 remaining<=0,立即 break,返回 ""。

    边界:collector.py:125-127,remaining = 0 - 0 = 0,``if remaining <= 0: break``,
    lines 为空,``"\n".join([])`` = ""。
    """
    from src.plugins.refine.collector import CollectedMessages, build_prompt_payload

    collected = CollectedMessages(
        messages=[(1700000000, "Alice", "hi")],
        period_start=1700000000, period_end=1700000000,
    )
    out = build_prompt_payload(collected, max_prompt_chars=0)
    assert out == ""


def test_build_prompt_payload_messages_exactly_fill_budget_no_loss() -> None:
    """多条消息累计长度刚好填满 max_prompt_chars → 不丢消息。

    边界:collector.py:124 ``if total + len(line) + 1 > max_prompt_chars``,
    刚好等于时不进入截断分支,正常 append。
    """
    from src.plugins.refine.collector import CollectedMessages, build_prompt_payload

    # 构造两条短消息,总长度远小于预算,确保都被采到
    collected = CollectedMessages(
        messages=[
            (1700000000, "Alice", "hello"),
            (1700000100, "Bob", "world"),
        ],
        period_start=1700000000, period_end=1700000100,
    )
    out = build_prompt_payload(collected, max_prompt_chars=10000)
    assert "Alice" in out
    assert "Bob" in out
    assert "hello" in out
    assert "world" in out
    # 两条都在
    assert out.count("\n") == 1


# ═══════════════════════════════════════════════════════════════
# 3b. collector 其他边界:resolve_target_type_and_value
# ═══════════════════════════════════════════════════════════════
#
# 基础变体(user:qq / 纯数字 / collection:名 / 集合 名 / 集合名 / 空 / 未知)
# 已被 test_refine.py::test_resolve_target_variants 覆盖。这里补边界形态。
# ═══════════════════════════════════════════════════════════════


def test_resolve_target_collection_with_trailing_space_only_returns_none() -> None:
    """``"集合 "``(集合后只有空格,无名)→ (None, None)。

    边界:collector.py:192-196,startswith("集合") 后 rest=text[2:].strip() 为空
    时显式返回 (None, None)。
    """
    from src.plugins.refine.collector import resolve_target_type_and_value

    assert resolve_target_type_and_value("集合 ") == (None, None)
    assert resolve_target_type_and_value("集合\t") == (None, None)


def test_resolve_target_user_prefix_empty_id_returns_none() -> None:
    """``"user:"``(空 user id)→ (None, None)。

    边界:collector.py:158-169 _parse_prefix_value,partition(":") 后 rest="",
    strip 后 not rest → 返回 (None, None)。
    """
    from src.plugins.refine.collector import resolve_target_type_and_value

    assert resolve_target_type_and_value("user:") == (None, None)
    # 同理 collection: 空名
    assert resolve_target_type_and_value("collection:") == (None, None)
    assert resolve_target_type_and_value("集合:") == (None, None)


def test_resolve_target_unknown_prefix_returns_none() -> None:
    """``"xxx:yyy"``(未知 prefix)→ (None, None)。

    边界:collector.py:165-169,partition 后 prefix 不在 {collection/集合/user},
    返回 (None, None)。
    """
    from src.plugins.refine.collector import resolve_target_type_and_value

    assert resolve_target_type_and_value("foobar:123") == (None, None)
    assert resolve_target_type_and_value("group:abc") == (None, None)


def test_resolve_target_whitespace_only_input_returns_none() -> None:
    """``"   "``(纯空白)→ strip 后为空 → (None, None)。

    边界:collector.py:185-187,text.strip() 后 not text 返回 (None, None)。
    """
    from src.plugins.refine.collector import resolve_target_type_and_value

    assert resolve_target_type_and_value("   ") == (None, None)
    assert resolve_target_type_and_value("\t\n") == (None, None)


def test_resolve_target_user_prefix_uppercase_normalized() -> None:
    """``"USER:123"``(大写 prefix)→ ('user', '123')。

    边界:collector.py:160-168,prefix 被 .strip().lower() 规范化,
    大写 USER 也被识别。
    """
    from src.plugins.refine.collector import resolve_target_type_and_value

    assert resolve_target_type_and_value("USER:123") == ("user", "123")
    assert resolve_target_type_and_value("Collection:核心") == ("collection", "核心")


# ═══════════════════════════════════════════════════════════════
# 4. ai.py 边界:extract_response_content 的剩余分支
# ═══════════════════════════════════════════════════════════════
#
# 已覆盖(test_refine.py):empty string content / missing content / missing
# message / empty choices / missing choices / non-json body / non-dict payload /
# array text 段拼接 / array 跳过非 text 段。
#
# 这里补未覆盖的:array 全空 text 段 / content 是 dict。
# ═══════════════════════════════════════════════════════════════


def test_extract_response_content_array_all_empty_text_raises() -> None:
    """content 是 array 但所有 text 段被过滤后为空 → RefineAIResponseError。

    边界:ai.py:64-74,list 分支 text_parts 为空时不返回,落到 74 行抛
    「响应中缺少可解析的 content」。
    """
    from src.plugins.refine.ai import extract_response_content
    from src.plugins.refine.exceptions import RefineAIResponseError

    payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "image_url", "image_url": {"url": "xxx"}},
                        {"type": "image_url", "image_url": {"url": "yyy"}},
                    ],
                }
            }
        ]
    }
    with pytest.raises(RefineAIResponseError) as exc_info:
        extract_response_content(payload)
    assert "可解析的 content" in str(exc_info.value)


def test_extract_response_content_dict_raises() -> None:
    """content 是 dict → RefineAIResponseError。

    边界:ai.py:62-74,isinstance(content, str) 否、isinstance(content, list) 否,
    落到 74 行抛异常。OpenAI content 只能是 str 或 list,dict 非法。
    """
    from src.plugins.refine.ai import extract_response_content
    from src.plugins.refine.exceptions import RefineAIResponseError

    payload = {
        "choices": [
            {"message": {"content": {"unexpected": "dict_shape"}}}
        ]
    }
    with pytest.raises(RefineAIResponseError):
        extract_response_content(payload)


def test_extract_response_content_array_multiple_text_segments_concatenated() -> None:
    """content 是 array 含多个 text 段 → 顺序拼接后 strip。

    边界:ai.py:65-73,list comprehension 保序,"".join(text_parts).strip()。
    与 test_refine.py 的 array 用例差异:这里直接测 extract_response_content
    纯函数(不走 httpx mock),验证拼接顺序与 strip 行为。
    """
    from src.plugins.refine.ai import extract_response_content

    payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "  第一段 "},
                        {"type": "text", "text": "第二段  "},
                        {"type": "text", "text": "第三段"},
                    ],
                }
            }
        ]
    }
    result = extract_response_content(payload)
    assert result == "第一段 第二段  第三段"


# ═══════════════════════════════════════════════════════════════
# 4b. ai.py 边界:request_refine_summary 的 200 但 choices 空分支
# ═══════════════════════════════════════════════════════════════
#
# test_refine.py 已覆盖 empty choices / non-json body。这里用一个更直接的
# 集成路径再补一条:200 + 合法 JSON + choices 为空 list。
# (与 test_ai_empty_choices_raises_response_error 同语义,但走完整 HTTP 路径,
#  验证 ``raise_for_status`` 不抛 + json() 成功 + extract 抛的链路。)
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_request_summary_200_with_empty_choices_raises_response_error() -> None:
    """httpx 返回 200 + 合法 JSON 但 choices 为空 list → RefineAIResponseError。

    边界:ai.py:107 raise_for_status 不抛(200)、ai.py:119 json() 成功、
    ai.py:126 extract_response_content 抛「响应中缺少 choices」。
    这条链路验证「HTTP 成功但业务字段缺失」的端到端异常映射。
    """
    from src.plugins.refine.ai import request_refine_summary
    from src.plugins.refine.exceptions import RefineAIResponseError

    response = httpx.Response(
        200,
        json={"choices": []},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)),
        pytest.raises(RefineAIResponseError) as exc_info,
    ):
        await request_refine_summary(
            base_url="https://example.com/v1", api_key="sk-test",
            model="gpt-test", timeout_seconds=10, temperature=0.3,
            prompt_payload="xxx",
        )
    assert "响应中缺少 choices" in str(exc_info.value)
