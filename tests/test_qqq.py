from datetime import datetime

import pytest
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender
from nonebug import App


def create_group_event(message: str, group_id: int = 123456, user_id: int = 111111) -> GroupMessageEvent:
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
        sender=Sender(user_id=user_id, nickname="测试用户", card="", role="member"),
    )


@pytest.mark.asyncio
async def test_qqq_matcher_rule_matches(app: App):
    """测试 qqq matcher 的规则能匹配 'qqq' 消息"""
    from src.plugins.qqq import qqq

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("qqq")

        result = await qqq.rule(bot, event, {})
        assert result is True, "qqq 消息应该匹配 qqq matcher 规则"


@pytest.mark.asyncio
async def test_qqq_matcher_rule_not_matches(app: App):
    """测试 qqq matcher 的规则不匹配其他消息"""
    from src.plugins.qqq import qqq

    async with app.test_matcher(qqq) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        event = create_group_event("hello")

        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule()


def test_parse_tencent_quote():
    """测试腾讯行情文本解析"""
    from src.plugins.qqq import _parse_tencent_quote

    fields = ["200", "纳指100ETF-Invesco", "QQQ.OQ", "732.07", "723.70", "725.15"]
    fields += ["0"] * 23
    fields += ["", "2026-08-13 16:00:01", "8.37", "1.16", "733.96", "724.03", "USD"]
    content = f'v_usQQQ="{"~".join(fields)}";'

    quote = _parse_tencent_quote(content.encode("gbk"))
    assert quote is not None, "腾讯行情解析不应返回 None"
    assert quote.name == "纳斯达克100ETF", "显示名应统一为纳斯达克100ETF"
    assert quote.price == 732.07
    assert quote.percent == 1.16
    assert quote.change == 8.37
    assert quote.prev_close == 723.70


def test_format_message_is_compact():
    """测试输出格式为简洁的 净值（涨幅）"""
    from src.plugins.qqq import QQQQuote

    quote = QQQQuote(
        name="纳斯达克100ETF",
        price=732.07,
        change=8.37,
        percent=1.16,
        open=725.15,
        high=733.96,
        low=724.03,
        prev_close=723.70,
    )
    assert quote.format_message() == "732.07（+1.16%）"


@pytest.mark.asyncio
async def test_fetch_qqq_quote_returns_valid_data():
    """测试获取 QQQ 行情函数能返回有效数据"""
    from src.plugins.qqq import fetch_qqq_quote

    quote = await fetch_qqq_quote()
    assert quote is not None, "QQQ 行情获取失败"
    assert quote.price > 0, f"价格应该是正数，实际是: {quote.price}"
