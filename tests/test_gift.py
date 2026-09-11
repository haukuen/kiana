"""随机礼物插件：协议编码、随机池与命令入口。"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender
from nonebug import App


def create_group_event(
    message: str,
    group_id: int = 123456,
    user_id: int = 111111,
    nickname: str = "测试用户",
    card: str = "",
) -> GroupMessageEvent:
    """创建群消息事件"""
    return GroupMessageEvent(
        time=int(datetime.now().timestamp()),
        self_id=987654321,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        group_id=group_id,
        message_id=1,
        message=Message(message),
        original_message=Message(message),
        raw_message=message,
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


@pytest.mark.asyncio
async def test_bot_nickname_is_cached(app: App) -> None:
    from src.plugins import gift as gift_plugin

    bot = MagicMock()
    bot.self_id = "987654321"
    bot.get_login_info = AsyncMock(return_value={"nickname": "Amadeus"})

    assert await gift_plugin._bot_nickname(bot) == "Amadeus"
    assert await gift_plugin._bot_nickname(bot) == "Amadeus"
    assert bot.get_login_info.await_count == 1


@pytest.mark.asyncio
async def test_bot_nickname_failure_is_not_cached(app: App) -> None:
    """查询失败时回退默认昵称，且不把失败结果缓存下来。"""
    from src.plugins import gift as gift_plugin

    bot = MagicMock()
    bot.self_id = "987654321"
    bot.get_login_info = AsyncMock(side_effect=RuntimeError("boom"))

    assert await gift_plugin._bot_nickname(bot) == "机器人"
    assert await gift_plugin._bot_nickname(bot) == "机器人"
    assert bot.get_login_info.await_count == 2


@pytest.mark.asyncio
async def test_gift_matcher_rule_matches(app: App) -> None:
    from src.plugins.gift import random_gift

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        assert await random_gift.rule(bot, create_group_event("随机礼物"), {}) is True


@pytest.mark.asyncio
async def test_gift_matcher_rule_not_matches(app: App) -> None:
    from src.plugins.gift import random_gift

    async with app.test_matcher(random_gift) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        ctx.receive_event(bot, create_group_event("随机礼物给我"))
        ctx.should_not_pass_rule()


@pytest.mark.asyncio
async def test_random_gift_sends_to_sender_without_reply(app: App, monkeypatch) -> None:
    """命令触发者就是收礼人，礼物取自配置的池；命令本身不发任何消息。"""
    from src.plugins import gift as gift_plugin

    monkeypatch.setattr(gift_plugin.config, "gift_pool", ["camping"])
    monkeypatch.setattr(gift_plugin, "_bot_nickname", AsyncMock(return_value="Amadeus"))
    sent = AsyncMock()
    monkeypatch.setattr(gift_plugin, "send_gift", sent)

    async with app.test_matcher(gift_plugin.random_gift) as ctx:
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
    assert kwargs["group_id"] == 123456
    assert kwargs["sender_qq"] == 987654321
    assert kwargs["sender_nickname"] == "Amadeus"


@pytest.mark.asyncio
async def test_random_gift_uses_card_as_target_nickname(app: App, monkeypatch) -> None:
    """群名片优先于昵称，用于礼物包里的 recvnickname。"""
    from src.plugins import gift as gift_plugin

    monkeypatch.setattr(gift_plugin.config, "gift_pool", ["camping"])
    monkeypatch.setattr(gift_plugin, "_bot_nickname", AsyncMock(return_value="Amadeus"))
    sent = AsyncMock()
    monkeypatch.setattr(gift_plugin, "send_gift", sent)

    async with app.test_matcher(gift_plugin.random_gift) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("随机礼物", card="群名片")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()

    assert sent.await_args.kwargs["target_nickname"] == "群名片"


@pytest.mark.asyncio
async def test_random_gift_empty_pool_sends_nothing(app: App, monkeypatch) -> None:
    """池全无效时安静地什么都不做，不回退到全部礼物（那会乱花真金币）。"""
    from src.plugins import gift as gift_plugin

    monkeypatch.setattr(gift_plugin.config, "gift_pool", ["不存在的礼物"])
    monkeypatch.setattr(gift_plugin, "_bot_nickname", AsyncMock(return_value="Amadeus"))
    sent = AsyncMock()
    monkeypatch.setattr(gift_plugin, "send_gift", sent)

    async with app.test_matcher(gift_plugin.random_gift) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("随机礼物")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()

    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_random_gift_cooldown_is_per_user(app: App, monkeypatch) -> None:
    """同一用户冷却内被拦，同群其他人不受影响。"""
    from src.plugins import gift as gift_plugin

    monkeypatch.setattr(gift_plugin.config, "gift_pool", ["camping"])
    monkeypatch.setattr(gift_plugin, "_bot_nickname", AsyncMock(return_value="Amadeus"))
    sent = AsyncMock()
    monkeypatch.setattr(gift_plugin, "send_gift", sent)

    def fire(ctx, *, user_id: int = 111111, nickname: str = "测试用户") -> None:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        reset_mute_cache()
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, create_group_event("随机礼物", user_id=user_id, nickname=nickname))
        ctx.should_pass_rule()

    async with app.test_matcher(gift_plugin.random_gift) as ctx:
        fire(ctx)
    assert sent.await_count == 1

    async with app.test_matcher(gift_plugin.random_gift) as ctx:
        fire(ctx)
    assert sent.await_count == 1, "同一用户第二次触发应被冷却拦下"

    async with app.test_matcher(gift_plugin.random_gift) as ctx:
        fire(ctx, user_id=222222, nickname="另一个人")
    assert sent.await_count == 2, "同群其他用户不应受该用户冷却影响"


@pytest.mark.asyncio
async def test_random_gift_failure_keeps_no_cooldown(app: App, monkeypatch) -> None:
    """送礼出错既不回消息，也不写冷却——一次抖动不该把用户锁 60 秒。"""
    from src.plugins import gift as gift_plugin

    monkeypatch.setattr(gift_plugin.config, "gift_pool", ["camping"])
    monkeypatch.setattr(gift_plugin, "_bot_nickname", AsyncMock(return_value="Amadeus"))
    monkeypatch.setattr(
        gift_plugin, "send_gift", AsyncMock(side_effect=RuntimeError("socket 断了"))
    )

    async with app.test_matcher(gift_plugin.random_gift) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("随机礼物")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()

    assert gift_plugin._cooldowns == {}


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
