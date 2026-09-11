"""随机礼物插件：协议编码、随机池与命令入口。"""

import itertools
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Sender
from nonebug import App

# message_id 必须每个事件唯一：nonebot-plugin-alconna 会按 message_id 缓存转换后的
# UniMessage，全用同一个 id 会让后一个用例拿到前一个用例的缓存，表现为随机的顺序依赖。
_message_ids = itertools.count(1)


@pytest.fixture(autouse=True)
def onebot11_uniseg_for_fake_adapter():
    """让 nonebug 的假 adapter 用真实的 OneBot11 uniseg builder/exporter。

    nonebug 的 ``SupportAdapter.nonebug`` 只实现了 text，at 段会被降级成 ``Other``，
    于是 Alconna 的 ``At`` 参数在测试里永远匹配不上。这里换成真实的 OneBot11 实现，
    跑的就是生产路径（同样是把 OneBot v11 的 Message 转成 uniseg）。

    必须改 ``BUILDER_MAPPING`` 而不是 ``loaders``：后者是懒查表，前者在导入时就把
    ``"fake"`` 预置成 nonebug 的实现了，改 loaders 不会生效。
    """
    from nonebot_plugin_alconna.uniseg.adapters import BUILDER_MAPPING, EXPORTER_MAPPING
    from nonebot_plugin_alconna.uniseg.adapters.onebot11 import Loader as OneBot11Loader
    from nonebot_plugin_alconna.uniseg.constraint import SupportAdapter

    key = SupportAdapter.nonebug.value
    loader = OneBot11Loader()
    old_builder, old_exporter = BUILDER_MAPPING.get(key), EXPORTER_MAPPING.get(key)
    BUILDER_MAPPING[key] = loader.get_builder()
    EXPORTER_MAPPING[key] = loader.get_exporter()
    yield
    BUILDER_MAPPING[key] = old_builder  # type: ignore[assignment]
    EXPORTER_MAPPING[key] = old_exporter  # type: ignore[assignment]


def create_group_event(
    message: str | Message,
    group_id: int = 123456,
    user_id: int = 111111,
    nickname: str = "测试用户",
    card: str = "",
) -> GroupMessageEvent:
    """创建群消息事件"""
    msg = message if isinstance(message, Message) else Message(message)
    return GroupMessageEvent(
        time=int(datetime.now().timestamp()),
        self_id=987654321,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        group_id=group_id,
        message_id=next(_message_ids),
        message=msg,
        original_message=msg,
        raw_message=str(msg),
        font=0,
        sender=Sender(user_id=user_id, nickname=nickname, card=card, role="member"),
    )


def expect_bot_not_muted(ctx, group_id: int = 123456, self_id: int = 987654321) -> None:
    """声明预期的禁言检查 API 调用"""
    ctx.should_call_api(
        "get_group_member_info",
        {"group_id": group_id, "user_id": self_id, "no_cache": True},
        result={"shut_up_timestamp": 0},
    )


def expect_member_name(
    ctx, group_id: int = 123456, user_id: int = 222222, card: str = "", nickname: str = "李四"
) -> None:
    """声明被 @ 的人的成员信息查询"""
    ctx.should_call_api(
        "get_group_member_info",
        {"group_id": group_id, "user_id": user_id},
        result={"card": card, "nickname": nickname},
    )


def reset_mute_cache() -> None:
    """清空全局禁言缓存，让每个事件都真的走一次 API。"""
    from src import plugins as global_plugins

    global_plugins._mute_cache.clear()


# 上游 bl-chat-plugin/SendGiftTool 在随机数固定为 987654321 时的真实输出，
# 用于把移植后的字节布局钉死（改坏任何一个字段都会在此暴露）。
UPSTREAM_PACKETS = [
    (
        "camping",
        111111,
        "测试",
        123456,
        "0a06120408c0c40712060801100018001a4a0a481246aa03430829123f08001206e99cb2e890"
        "a51887e4062206e6b58be8af9528b1d1f9d6033207416d6164657573388403420050e70f5805"
        "620233306a0a0a03108403100920840320b1d1f9d60328b1d1f9d603",
    ),
    (
        "champagne",
        222222,
        "张三",
        654321,
        "0a06120408f1f72712060801100018001a4c0a4a1248aa03450829124108001206e9a699e6a7"
        "9f188ec80d2206e5bca0e4b88928b1d1f9d6033207416d616465757338bf9a0c420050831158"
        "05620233306a0b0a0310b601100920bf9a0c20b1d1f9d60328b1d1f9d603",
    ),
    (
        "helicopter",
        333333,
        "李四",
        0,
        "0a041202080012060801100018001a530a51124faa034c0829124808001209e79bb4e58d87"
        "e69cba1895ac142206e69d8ee59b9b28b1d1f9d6033207416d616465757338c89301420050ee"
        "94ebdc035805620233306a0c0a0410c09301100920c8930120b1d1f9d60328b1d1f9d603",
    ),
    (
        "dragon",
        444444,
        "Alice",
        999,
        "0a05120308e70712060801100018001a510a4f124daa034a082912460800120ce9be99e885be"
        "e4b887e9878c189c901b2205416c69636528b1d1f9d6033207416d616465757338bf9a0c4200"
        "50e10c5805620233306a0b0a0310f05c100920bf9a0c20b1d1f9d60328b1d1f9d603",
    ),
]


def test_encode_message_wire_format() -> None:
    """varint / 长度前缀 / 空嵌套消息三种 wire 形态的字节布局。"""
    from src.plugins.gift.protocol import encode_message

    assert encode_message({1: 41}) == b"\x08\x29"
    assert encode_message({2: "香槟"}) == b"\x12\x06" + "香槟".encode()
    assert encode_message({8: {}}) == b"\x42\x00"
    assert encode_message({1: 300}) == b"\x08\xac\x02"
    # tag 53 需要两字节 varint，嵌套消息按长度前缀展开
    assert encode_message({53: {1: 41}}) == b"\xaa\x03\x02\x08\x29"
    # tag 必须升序，且与 dict 插入顺序无关
    assert encode_message({2: "a", 1: 41}) == b"\x08\x29\x12\x01a"


def test_encode_message_rejects_unknown_type() -> None:
    from src.plugins.gift.protocol import encode_message

    with pytest.raises(TypeError):
        encode_message({1: 1.5})  # type: ignore[dict-item]


def test_build_gift_packet_matches_upstream(monkeypatch) -> None:
    """构造出的数据包与上游 JS 逐字节一致。"""
    from src.plugins.gift import protocol
    from src.plugins.gift.gifts import GIFTS

    monkeypatch.setattr(protocol.random, "getrandbits", lambda _bits: 987654321)

    for key, target_qq, target_nickname, group_id, expected in UPSTREAM_PACKETS:
        actual = protocol.build_gift_packet(
            gift=GIFTS[key],
            group_id=group_id,
            target_qq=target_qq,
            target_nickname=target_nickname,
            sender_qq=987654321,
            sender_nickname="Amadeus",
        ).hex()
        assert actual == expected, f"{key} 的数据包与上游不一致"


def test_available_gifts_defaults_to_all() -> None:
    from src.plugins.gift.gifts import GIFTS, available_gifts

    assert [g.key for g in available_gifts([])] == list(GIFTS)


def test_available_gifts_filters_unknown_and_duplicates() -> None:
    from src.plugins.gift.gifts import available_gifts

    pool = available_gifts(["camping", "不存在的礼物", "camping", "dragon"])
    assert [g.key for g in pool] == ["camping", "dragon"]

    assert available_gifts(["全都不认识"]) == []


def test_pick_gift_returns_none_for_empty_pool() -> None:
    from src.plugins.gift.gifts import pick_gift

    assert pick_gift(["全都不认识"]) is None
    assert pick_gift(["camping"]).key == "camping"


# ==================== 命令词匹配 ====================


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["随机礼物", "送礼物"])
async def test_gift_matcher_rule_matches(app: App, text: str) -> None:
    from src.plugins import gift as gift_plugin

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        assert await gift_plugin.gift_matcher.rule(bot, create_group_event(text), {}) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["随机礼物", "送礼物"])
@pytest.mark.parametrize("gap", ["", " "])
async def test_gift_matcher_rule_matches_with_trailing_at(app: App, text: str, gap: str) -> None:
    """回归：at 段不进纯文本，所以「送礼物 @某人」的纯文本是「送礼物 」带尾空格。

    曾经用 on_fullmatch，而 FullmatchRule 是精确相等匹配，带空格就完全匹配不上。
    """
    from src.plugins import gift as gift_plugin

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event(Message(f"{text}{gap}") + MessageSegment.at(222222))
        assert await gift_plugin.gift_matcher.rule(bot, event, {}) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["送礼物给我", "礼物", "随机礼物 香槟", "送我礼物"])
async def test_gift_matcher_rule_not_matches(app: App, text: str) -> None:
    from src.plugins import gift as gift_plugin

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        assert await gift_plugin.gift_matcher.rule(bot, create_group_event(text), {}) is False


# ==================== 收礼人与署名 ====================


@pytest.mark.asyncio
async def test_gift_goes_to_command_sender_without_reply(app: App, monkeypatch) -> None:
    """不带 @ 时收礼人就是发命令的人，且署名也是他（不是 bot）；命令不发任何消息。"""
    from src.plugins import gift as gift_plugin

    monkeypatch.setattr(gift_plugin.config, "gift_pool", ["camping"])
    sent = AsyncMock()
    monkeypatch.setattr(gift_plugin, "send_gift", sent)

    async with app.test_matcher(gift_plugin.gift_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("随机礼物")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        # 故意不声明 should_call_send：nonebug 遇到没预期的发送会直接 pytest.fail，
        # 所以"整段跑完不报错"本身就是"没有发出任何消息"的断言。

    kwargs = sent.await_args.kwargs
    assert kwargs["gift"].key == "camping"
    assert kwargs["target_qq"] == 111111
    assert kwargs["sender_qq"] == 111111, "署名应该是发命令的人，不是 bot"
    assert kwargs["sender_nickname"] == "测试用户"
    assert kwargs["group_id"] == 123456


@pytest.mark.asyncio
async def test_gift_goes_to_mentioned_member(app: App, monkeypatch) -> None:
    """「送礼物@某人」把礼物送给被 @ 的人，收礼人昵称取自群名片。"""
    from src.plugins import gift as gift_plugin

    monkeypatch.setattr(gift_plugin.config, "gift_pool", ["camping"])
    sent = AsyncMock()
    monkeypatch.setattr(gift_plugin, "send_gift", sent)

    async with app.test_matcher(gift_plugin.gift_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event(Message("送礼物 ") + MessageSegment.at(222222))
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        expect_member_name(ctx, card="群名片李四")

    kwargs = sent.await_args.kwargs
    assert kwargs["target_qq"] == 222222
    assert kwargs["target_nickname"] == "群名片李四"
    assert kwargs["sender_qq"] == 111111, "署名仍是发命令的人"
    assert kwargs["sender_nickname"] == "测试用户"


@pytest.mark.asyncio
async def test_mentioned_member_without_card_falls_back_to_nickname(app: App, monkeypatch) -> None:
    from src.plugins import gift as gift_plugin

    monkeypatch.setattr(gift_plugin.config, "gift_pool", ["camping"])
    sent = AsyncMock()
    monkeypatch.setattr(gift_plugin, "send_gift", sent)

    async with app.test_matcher(gift_plugin.gift_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event(Message("送礼物") + MessageSegment.at(222222))
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        expect_member_name(ctx, card="", nickname="李四")

    assert sent.await_args.kwargs["target_nickname"] == "李四"


@pytest.mark.asyncio
async def test_member_lookup_failure_falls_back_to_qq(app: App, monkeypatch) -> None:
    """取昵称失败不该让整次送礼泡汤，退回 QQ 号继续。"""
    from src.plugins import gift as gift_plugin

    monkeypatch.setattr(gift_plugin.config, "gift_pool", ["camping"])
    sent = AsyncMock()
    monkeypatch.setattr(gift_plugin, "send_gift", sent)

    async with app.test_matcher(gift_plugin.gift_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event(Message("送礼物 ") + MessageSegment.at(222222))
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_api(
            "get_group_member_info",
            {"group_id": 123456, "user_id": 222222},
            exception=RuntimeError("取成员信息失败"),
        )

    assert sent.await_args.kwargs["target_qq"] == 222222
    assert sent.await_args.kwargs["target_nickname"] == "222222"


@pytest.mark.asyncio
async def test_mention_all_is_ignored(app: App, monkeypatch) -> None:
    """@全体成员（qq=all）不是收礼人，退回发给自己，也不查成员信息。"""
    from src.plugins import gift as gift_plugin

    monkeypatch.setattr(gift_plugin.config, "gift_pool", ["camping"])
    sent = AsyncMock()
    monkeypatch.setattr(gift_plugin, "send_gift", sent)

    async with app.test_matcher(gift_plugin.gift_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event(Message("送礼物 ") + MessageSegment.at("all"))
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()

    assert sent.await_args.kwargs["target_qq"] == 111111


@pytest.mark.asyncio
async def test_mentioning_self_targets_self(app: App, monkeypatch) -> None:
    """@ 自己等效于不 @，不用去查自己的成员信息。"""
    from src.plugins import gift as gift_plugin

    monkeypatch.setattr(gift_plugin.config, "gift_pool", ["camping"])
    sent = AsyncMock()
    monkeypatch.setattr(gift_plugin, "send_gift", sent)

    async with app.test_matcher(gift_plugin.gift_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event(Message("送礼物 ") + MessageSegment.at(111111))
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()

    assert sent.await_args.kwargs["target_qq"] == 111111
    assert sent.await_args.kwargs["target_nickname"] == "测试用户"


@pytest.mark.asyncio
async def test_sender_uses_group_card(app: App, monkeypatch) -> None:
    """署名优先用群名片。"""
    from src.plugins import gift as gift_plugin

    monkeypatch.setattr(gift_plugin.config, "gift_pool", ["camping"])
    sent = AsyncMock()
    monkeypatch.setattr(gift_plugin, "send_gift", sent)

    async with app.test_matcher(gift_plugin.gift_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("送礼物", card="发言人名片")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()

    assert sent.await_args.kwargs["sender_nickname"] == "发言人名片"


# ==================== 冷却与失败 ====================


@pytest.mark.asyncio
async def test_random_gift_empty_pool_sends_nothing(app: App, monkeypatch) -> None:
    """池全无效时安静地什么都不做，不回退到全部礼物（那会乱花真金币）。"""
    from src.plugins import gift as gift_plugin

    monkeypatch.setattr(gift_plugin.config, "gift_pool", ["不存在的礼物"])
    sent = AsyncMock()
    monkeypatch.setattr(gift_plugin, "send_gift", sent)

    async with app.test_matcher(gift_plugin.gift_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("随机礼物")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()

    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_cooldown_is_per_sender_not_per_target(app: App, monkeypatch) -> None:
    """冷却按发命令的人算：同一个人换着 @ 别人也不能绕开。"""
    from src.plugins import gift as gift_plugin

    monkeypatch.setattr(gift_plugin.config, "gift_pool", ["camping"])
    sent = AsyncMock()
    monkeypatch.setattr(gift_plugin, "send_gift", sent)

    async def fire(ctx, event_factory) -> None:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        reset_mute_cache()
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event_factory())
        ctx.should_pass_rule()

    async with app.test_matcher(gift_plugin.gift_matcher) as ctx:
        await fire(ctx, lambda: create_group_event("随机礼物"))
    assert sent.await_count == 1

    # 同一个人，换成 @ 别人 —— 仍应被自己的冷却拦住
    async with app.test_matcher(gift_plugin.gift_matcher) as ctx:
        await fire(ctx, lambda: create_group_event(Message("送礼物 ") + MessageSegment.at(222222)))
    assert sent.await_count == 1, "换收礼人不该绕开冷却"

    # 换个人，不该受前一个人的冷却影响
    async with app.test_matcher(gift_plugin.gift_matcher) as ctx:
        await fire(ctx, lambda: create_group_event("随机礼物", user_id=222222, nickname="另一个人"))
    assert sent.await_count == 2


@pytest.mark.asyncio
async def test_gift_failure_keeps_no_cooldown(app: App, monkeypatch) -> None:
    """送礼出错既不回消息，也不写冷却——一次抖动不该把用户锁 60 秒。"""
    from src.plugins import gift as gift_plugin

    monkeypatch.setattr(gift_plugin.config, "gift_pool", ["camping"])
    monkeypatch.setattr(gift_plugin, "send_gift", AsyncMock(side_effect=RuntimeError("socket 断了")))

    async with app.test_matcher(gift_plugin.gift_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("随机礼物")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()

    assert gift_plugin._cooldowns == {}


# ==================== 发包兜底 ====================


def _action_failed(retcode: int):
    from nonebot.adapters.onebot.v11.exception import ActionFailed

    return ActionFailed(status="failed", retcode=retcode, message="不支持的API")


@pytest.mark.asyncio
async def test_send_gift_falls_back_to_send_pb() -> None:
    """action 不存在时退回 LLBot 的 send_pb，且沿用同一份 payload。"""
    from src.plugins.gift import protocol
    from src.plugins.gift.gifts import GIFTS

    calls: list[tuple[str, dict]] = []

    async def fake_call_api(api: str, **data):
        calls.append((api, data))
        if api == "send_packet":
            raise _action_failed(protocol.UNSUPPORTED_ACTION_RETCODE)
        return {}

    bot = MagicMock()
    bot.call_api = fake_call_api

    await protocol.send_gift(
        bot,
        gift=GIFTS["camping"],
        group_id=1,
        target_qq=2,
        target_nickname="甲",
        sender_qq=3,
        sender_nickname="乙",
    )

    assert [api for api, _ in calls] == ["send_packet", "send_pb"]
    assert calls[0][1] == {"cmd": protocol.PB_CMD, "data": calls[1][1]["hex"]}
    assert calls[1][1]["cmd"] == protocol.PB_CMD


@pytest.mark.asyncio
async def test_send_gift_does_not_retry_real_failures() -> None:
    """真实发送失败（非 1404）必须原样抛出，避免重复投递礼物。"""
    from nonebot.adapters.onebot.v11.exception import ActionFailed
    from src.plugins.gift import protocol
    from src.plugins.gift.gifts import GIFTS

    calls: list[str] = []

    async def fake_call_api(api: str, **data):
        calls.append(api)
        raise _action_failed(1200)

    bot = MagicMock()
    bot.call_api = fake_call_api

    with pytest.raises(ActionFailed):
        await protocol.send_gift(
            bot,
            gift=GIFTS["camping"],
            group_id=1,
            target_qq=2,
            target_nickname="甲",
            sender_qq=3,
            sender_nickname="乙",
        )

    assert calls == ["send_packet"]
