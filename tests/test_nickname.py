"""测试 un_nickname 插件的 at 昵称替换功能。

重点覆盖 AT_NICKNAME_PATTERN 与 is_replacing_nickname 的一致性，
以及此前中文标点/贪婪吞字等回归场景。
"""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import (
    Bot as OneBotV11Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.adapters.onebot.v11.event import Reply
from nonebug import App


def _make_event(content: str | Message) -> GroupMessageEvent:
    """构造群消息事件"""
    message = Message(content)
    return GroupMessageEvent(
        time=int(datetime.now().timestamp()),
        self_id=987654321,
        post_type="message",
        sub_type="normal",
        user_id=111,
        message_type="group",
        group_id=222,
        message_id=1,
        message=message,
        original_message=message,
        raw_message=str(message),
        font=0,
        sender={"user_id": 111, "nickname": "u", "card": "", "role": "member"},  # type: ignore[arg-type]
    )


def _action_failed() -> ActionFailed:
    return ActionFailed(
        status="failed",
        retcode=1200,
        data=None,
        message="Get Uid Error",
        wording="Get Uid Error",
    )


@pytest.mark.asyncio
async def test_reply_add_nickname_keeps_replied_user_mention(app: App) -> None:
    """适配器清理引用段后，昵称命令仍应使用原消息中的 @目标。"""
    from src.plugins.un_nickname.db import fetch_user_nicknames
    from src.plugins.un_nickname.handlers import add_nickname_matcher

    original_message = Message(
        [
            MessageSegment.reply(9),
            MessageSegment.at("999"),
            MessageSegment.text(" 昵称 老张"),
        ]
    )
    event = _make_event(original_message)
    event.reply = Reply(
        time=event.time,
        message_type="group",
        message_id=9,
        real_id=9,
        sender={"user_id": 999},
        message=Message("原消息"),
    )
    event.message = Message("昵称 老张")

    async with app.test_matcher(add_nickname_matcher) as ctx:
        bot = ctx.create_bot(base=OneBotV11Bot, self_id="987654321")
        ctx.should_call_api(
            "get_group_member_info",
            {"group_id": 222, "user_id": 987654321, "no_cache": True},
            result={"shut_up_timestamp": 0},
        )
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event, "昵称'老张'成功绑定到用户!", result={"message_id": 2}
        )

    assert await fetch_user_nicknames("222", "999") == ["老张"]


# ============== AT_NICKNAME_PATTERN 单元测试 ==============


@pytest.mark.parametrize(
    "text,expected_names",
    [
        # 基本场景
        ("at老张", ["老张"]),
        ("at 老张", ["老张"]),
        ("at老张 吃饭", ["老张"]),
        # 中文标点后跟（B1 回归）：以前 (?=\s|$) 全部失败
        ("at老张，吃饭吗", ["老张"]),
        ("at老张。", ["老张"]),
        ("at老张！", ["老张"]),
        ("at老张？", ["老张"]),
        # 句中、句尾
        ("喂 at老张 出来", ["老张"]),
        ("喂 at老张", ["老张"]),
        # 多个昵称
        ("at老张 at小李", ["老张", "小李"]),
        # 拉丁/数字昵称
        ("at abc 吃饭", ["abc"]),
        ("at dev1 出事了", ["dev1"]),
    ],
)
def test_at_nickname_pattern_matches(text: str, expected_names: list[str]) -> None:
    """正则应正确捕获名字，且不吞掉后续中文

    注意：纯字符正则无法做中文分词，'at老张吃饭' 这种无边界写法无法区分
    昵称与后续正文，需用空格或标点分隔。这是固有设计契约。
    """
    from src.plugins.un_nickname.utils import AT_NICKNAME_PATTERN

    matches = AT_NICKNAME_PATTERN.findall(text)
    assert matches == expected_names, f"文本 {text!r} 期望 {expected_names}，实际 {matches}"


@pytest.mark.parametrize(
    "text",
    [
        "that老张",  # 前面是字母，不构成 \b
        "battle today",
        "what is that",
        "look at",  # at 后没有名字字符
        "at，",  # at 后直接是中文标点，没有名字
        "AT老张",  # 大小写敏感，不匹配
        "At老张",
    ],
)
def test_at_nickname_pattern_no_match(text: str) -> None:
    """非 at 昵称语法不应被匹配"""
    from src.plugins.un_nickname.utils import AT_NICKNAME_PATTERN

    assert AT_NICKNAME_PATTERN.search(text) is None, f"文本 {text!r} 不应被匹配"


def test_at_nickname_pattern_consecutive_latin() -> None:
    """连写拉丁昵称（B3 回归）：'at dev at test' 在带空格时应分别命中"""
    from src.plugins.un_nickname.utils import AT_NICKNAME_PATTERN

    # 空格分隔的连写：第一段 "at dev" 命中 dev，剩余 " at test" 命中 test
    matches = AT_NICKNAME_PATTERN.findall("at dev at test 出事了")
    assert matches == ["dev", "test"]


# ============== is_replacing_nickname rule 一致性 ==============


@pytest.mark.parametrize(
    "text,should_trigger",
    [
        # 与正则一致：命中（昵称后有明确边界）
        ("at老张", True),
        ("at老张，吃饭吗", True),
        ("at老张 吃饭", True),
        # 与正则一致：不命中
        ("that老张", False),
        ("battle", False),
        ("what is that", False),
        ("look at", False),
    ],
)
def test_is_replacing_nickname_consistency(text: str, should_trigger: bool) -> None:
    """rule 必须与 AT_NICKNAME_PATTERN 判定一致，避免假阳性空跑 handler"""
    from src.plugins.un_nickname.handlers import is_replacing_nickname

    assert is_replacing_nickname(_make_event(text)) is should_trigger


# ============== handler 行为冒烟 ==============


# ============== 昵称列表切分（合并转发分节点） ==============


@pytest.mark.parametrize(
    "nicknames,max_chars,expected_groups",
    [
        # 少量昵称：单段
        (["老张", "小李"], 1000, [["老张", "小李"]]),
        # 超出单段上限：按累计长度切分
        (["a" * 10] * 3, 21, [["a" * 10], ["a" * 10], ["a" * 10]]),
        (["a" * 10, "b" * 10], 21, [["a" * 10], ["b" * 10]]),
        (["a" * 10, "b" * 10], 22, [["a" * 10, "b" * 10]]),
        # 分隔符 ", "（2 字符）计入长度：10 + 2 + 10 = 22 为临界
        (["a" * 10, "b" * 10], 21, [["a" * 10], ["b" * 10]]),
        (["a" * 10, "b" * 10], 22, [["a" * 10, "b" * 10]]),
        # 单个昵称超上限：独占一段，不强制截断
        (["a" * 30], 10, [["a" * 30]]),
        ([], 1000, []),
    ],
)
def test_split_nickname_list(
    nicknames: list[str], max_chars: int, expected_groups: list[list[str]]
) -> None:
    """应按 join 后的累计长度切分，且每段 join 结果不超过上限（单昵称除外）"""
    from src.plugins.un_nickname.utils import split_nickname_list

    groups = split_nickname_list(nicknames, max_chars)
    assert groups == expected_groups
    for group in groups:
        assert len(", ".join(group)) <= max_chars or len(group) == 1


def test_split_nickname_list_default_limit() -> None:
    """默认上限下，超长昵称列表应切分为多段且每段不超限"""
    from src.plugins.un_nickname.utils import FORWARD_NODE_MAX_CHARS, split_nickname_list

    # 300 个 15 字昵称 + 分隔符，join 后约 5100 字符，远超单节点上限
    nicknames = [f"昵称{i:03d}" + "x" * 10 for i in range(300)]
    groups = split_nickname_list(nicknames)
    assert len(groups) > 1
    assert [name for group in groups for name in group] == nicknames
    for group in groups:
        assert len(", ".join(group)) <= FORWARD_NODE_MAX_CHARS


@pytest.mark.asyncio
async def test_send_nickname_list_splits_forward_nodes(app: App) -> None:
    """超长昵称列表的合并转发应按长度切分为多个节点，避免单节点超过消息上限"""
    from unittest.mock import AsyncMock

    from src.plugins.un_nickname import handlers
    from src.plugins.un_nickname.utils import FORWARD_NODE_MAX_CHARS

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=OneBotV11Bot, self_id="987654321")
        event = _make_event("查看")
        nicknames = [f"昵称{i:03d}" + "x" * 10 for i in range(300)]

        call_api = AsyncMock(return_value=None)
        bot.call_api = call_api  # type: ignore[method-assign]

        await handlers._send_nickname_list(bot, event, nicknames, "该用户的昵称:", "昵称列表")

        call_api.assert_awaited_once()
        nodes = call_api.await_args.kwargs["messages"]
        assert len(nodes) > 1
        assert all(node["type"] == "node" for node in nodes)
        for node in nodes:
            content = node["data"]["content"]
            assert len(content) <= FORWARD_NODE_MAX_CHARS
        # 所有昵称按原顺序出现在节点中
        joined = ", ".join(node["data"]["content"] for node in nodes)
        assert all(name in joined for name in nicknames)


@pytest.mark.asyncio
async def test_replace_handler_no_match_skips_send(app: App) -> None:
    """无候选名时 handler 应早返回，不查询缓存、不发送消息"""
    from src.plugins.un_nickname import handlers
    from src.plugins.un_nickname.cache import _nickname_cache

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=OneBotV11Bot, self_id="987654321")
        event = _make_event("that老张 battle today")

        # 无 at 昵称命中，handler 应在收集候选阶段就 return，
        # 不应触发任何缓存查询（不写 _nickname_cache）或调用 send API
        _nickname_cache.clear()
        await handlers.handle_replace_nickname(bot, event)
        # 缓存未被写入，证明未走到 get_cached_nickname_map 分支
        assert "222" not in _nickname_cache


@pytest.mark.asyncio
async def test_replace_handler_success_skips_member_query(app: App, monkeypatch) -> None:
    """首次发送成功时不应查询群成员列表。"""
    from src.plugins.un_nickname import handlers

    monkeypatch.setattr(
        handlers, "get_cached_nickname_map", AsyncMock(return_value={"老张": "333"})
    )
    monkeypatch.setattr(handlers, "get_cached_collection_map", AsyncMock(return_value={}))

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=OneBotV11Bot, self_id="987654321")
        event = _make_event("at老张")
        ctx.should_call_send(event, Message(MessageSegment.at("333")), bot=bot)

        await handlers.handle_replace_nickname(bot, event)


@pytest.mark.asyncio
async def test_replace_handler_filters_departed_targets_and_retries(app: App, monkeypatch) -> None:
    """发送失败后应同时过滤昵称和集合中的非群成员。"""
    from src.plugins.un_nickname import handlers

    monkeypatch.setattr(
        handlers, "get_cached_nickname_map", AsyncMock(return_value={"老张": "333"})
    )
    monkeypatch.setattr(
        handlers,
        "get_cached_collection_map",
        AsyncMock(return_value={"小组": ["333", "444"]}),
    )

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=OneBotV11Bot, self_id="987654321")
        event = _make_event("at老张，at小组 出发")
        ctx.should_call_send(
            event,
            Message(
                [
                    MessageSegment.at("333"),
                    MessageSegment.text("，"),
                    MessageSegment.at("333"),
                    MessageSegment.at("444"),
                    MessageSegment.text(" 出发"),
                ]
            ),
            exception=_action_failed(),
            bot=bot,
        )
        ctx.should_call_api(
            "get_group_member_list",
            {"group_id": 222, "no_cache": True},
            result=[{"user_id": 111}, {"user_id": 444}],
        )
        ctx.should_call_send(
            event,
            Message(
                [
                    MessageSegment.text("at老张"),
                    MessageSegment.text("，"),
                    MessageSegment.at("444"),
                    MessageSegment.text(" 出发"),
                ]
            ),
            bot=bot,
        )

        await handlers.handle_replace_nickname(bot, event)


@pytest.mark.asyncio
async def test_replace_handler_all_targets_departed_stays_silent(app: App, monkeypatch) -> None:
    """过滤后没有有效目标时不应再次发送。"""
    from src.plugins.un_nickname import handlers

    monkeypatch.setattr(
        handlers, "get_cached_nickname_map", AsyncMock(return_value={"老张": "333"})
    )
    monkeypatch.setattr(handlers, "get_cached_collection_map", AsyncMock(return_value={}))

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=OneBotV11Bot, self_id="987654321")
        event = _make_event("at老张")
        ctx.should_call_send(
            event,
            Message(MessageSegment.at("333")),
            exception=_action_failed(),
            bot=bot,
        )
        ctx.should_call_api(
            "get_group_member_list",
            {"group_id": 222, "no_cache": True},
            result=[{"user_id": 111}],
        )

        await handlers.handle_replace_nickname(bot, event)


@pytest.mark.asyncio
async def test_replace_handler_member_query_failure_stays_silent(app: App, monkeypatch) -> None:
    """成员列表查询失败时不应继续发送或向外抛错。"""
    from src.plugins.un_nickname import handlers

    monkeypatch.setattr(
        handlers, "get_cached_nickname_map", AsyncMock(return_value={"老张": "333"})
    )
    monkeypatch.setattr(handlers, "get_cached_collection_map", AsyncMock(return_value={}))

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=OneBotV11Bot, self_id="987654321")
        event = _make_event("at老张")
        ctx.should_call_send(
            event,
            Message(MessageSegment.at("333")),
            exception=_action_failed(),
            bot=bot,
        )
        ctx.should_call_api(
            "get_group_member_list",
            {"group_id": 222, "no_cache": True},
            exception=RuntimeError("query failed"),
        )

        await handlers.handle_replace_nickname(bot, event)


@pytest.mark.asyncio
async def test_replace_handler_retry_failure_stays_silent(app: App, monkeypatch) -> None:
    """过滤后的单次重试仍失败时不应向外抛错。"""
    from src.plugins.un_nickname import handlers

    monkeypatch.setattr(handlers, "get_cached_nickname_map", AsyncMock(return_value={}))
    monkeypatch.setattr(
        handlers,
        "get_cached_collection_map",
        AsyncMock(return_value={"小组": ["333", "444"]}),
    )

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=OneBotV11Bot, self_id="987654321")
        event = _make_event("at小组")
        ctx.should_call_send(
            event,
            Message([MessageSegment.at("333"), MessageSegment.at("444")]),
            exception=_action_failed(),
            bot=bot,
        )
        ctx.should_call_api(
            "get_group_member_list",
            {"group_id": 222, "no_cache": True},
            result=[{"user_id": 444}],
        )
        ctx.should_call_send(
            event,
            Message(MessageSegment.at("444")),
            exception=_action_failed(),
            bot=bot,
        )

        await handlers.handle_replace_nickname(bot, event)


@pytest.mark.asyncio
async def test_replace_handler_unrelated_send_failure_does_not_retry(app: App, monkeypatch) -> None:
    """目标均在群内时应将发送失败视为其他故障，不再发送。"""
    from src.plugins.un_nickname import handlers

    monkeypatch.setattr(
        handlers, "get_cached_nickname_map", AsyncMock(return_value={"老张": "333"})
    )
    monkeypatch.setattr(handlers, "get_cached_collection_map", AsyncMock(return_value={}))

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=OneBotV11Bot, self_id="987654321")
        event = _make_event("at老张")
        ctx.should_call_send(
            event,
            Message(MessageSegment.at("333")),
            exception=_action_failed(),
            bot=bot,
        )
        ctx.should_call_api(
            "get_group_member_list",
            {"group_id": 222, "no_cache": True},
            result=[{"user_id": 111}, {"user_id": 333}],
        )

        await handlers.handle_replace_nickname(bot, event)
