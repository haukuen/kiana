"""测试 fund 插件的 matcher 行为"""

import pytest
from datetime import datetime
from nonebug import App
from nonebot.adapters.onebot.v11 import Bot, Message, GroupMessageEvent
from unittest.mock import patch


def create_fake_group_message_event(
    message: str,
    user_id: int = 123456,
    group_id: int = 654321,
    self_id: int = 987654321,
) -> GroupMessageEvent:
    """创建假的群消息事件用于测试"""
    return GroupMessageEvent(
        time=int(datetime.now().timestamp()),
        self_id=self_id,
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
        sender={
            "user_id": user_id,
            "nickname": "测试用户",
            "card": "",
            "role": "member",
        },
    )


@pytest.mark.asyncio
async def test_fund_query_matcher_regex_match(app: App):
    """测试 fund_query matcher 的正则匹配"""
    from src.plugins.fund import fund_query
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=OneBotV11Bot, self_id="987654321")
        event = create_fake_group_message_event("002170")

        result = await fund_query.rule(bot, event, {})
        assert result is True


@pytest.mark.asyncio
async def test_fund_query_matcher_regex_not_match(app: App):
    """测试不匹配正则的消息"""
    from src.plugins.fund import fund_query

    async with app.test_matcher(fund_query) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")

        event = create_fake_group_message_event("hello world")
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule()


@pytest.mark.asyncio
async def test_fund_query_matcher_etf_code(app: App):
    """测试 ETF 代码的正则匹配"""
    from src.plugins.fund import fund_query

    async with app.test_matcher(fund_query) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")

        event = create_fake_group_message_event("510300")
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()


@pytest.mark.asyncio
async def test_fund_query_matcher_stock_code_with_suffix(app: App):
    """测试带交易所后缀的股票代码匹配"""
    from src.plugins.fund import fund_query
    from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=OneBotV11Bot, self_id="987654321")

        # 测试深圳股票代码
        event = create_fake_group_message_event("000001.SZ")
        result = await fund_query.rule(bot, event, {})
        assert result is True

        # 测试上海股票代码
        event2 = create_fake_group_message_event("600000.SH")
        result2 = await fund_query.rule(bot, event2, {})
        assert result2 is True


@pytest.mark.asyncio
async def test_fund_query_matcher_beijing_stock(app: App):
    """测试北交所股票代码匹配"""
    from src.plugins.fund import fund_query

    async with app.test_matcher(fund_query) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")

        event = create_fake_group_message_event("43123456.BJ")
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()


@pytest.mark.asyncio
async def test_fund_query_handler_with_mocked_data(app: App):
    """测试 fund_query handler 的完整流程"""
    from src.plugins.fund import fund_query

    # 准备期望的返回文本
    expected_info_text = """东吴移动互联混合C
代码: 002170

最近交易日收益:
2024-01-03: +1.20%
2024-01-02: -0.30%
2024-01-01: +0.50%

阶段收益:
近1月: 5.20%
近3月: -2.10%
近6月: 8.70%
近1年: 15.30%"""

    with patch("src.plugins.fund.query_by_code_type") as mock_query:
        mock_query.return_value = (expected_info_text, None)

        async with app.test_matcher(fund_query) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            event = create_fake_group_message_event("002170")

            ctx.receive_event(bot, event)
            ctx.should_pass_rule()

            # 声明预期的 API 调用
            ctx.should_call_api(
                "send_group_forward_msg",
                {
                    "group_id": 654321,
                    "messages": [
                        {
                            "type": "node",
                            "data": {"name": "", "uin": "987654321", "content": expected_info_text},
                        }
                    ],
                },
                result={"message_id": 999},
            )


@pytest.mark.asyncio
async def test_fund_query_handler_with_unknown_code(app: App):
    """测试未知代码类型的处理（静默失败）"""
    from src.plugins.fund import fund_query, identify_code_type, CodeType

    async with app.test_matcher(fund_query) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")

        # 使用一个匹配正则但无法识别类型的代码
        # 由于正则只允许特定格式，这里我们 mock identify_code_type
        with patch("src.plugins.fund.identify_code_type") as mock_identify:
            mock_identify.return_value = CodeType.UNKNOWN

            event = create_fake_group_message_event("000000")
            ctx.receive_event(bot, event)
            ctx.should_pass_rule()

            # 未知代码应该静默失败，不发送任何消息
            # 不应该有 API 调用


@pytest.mark.asyncio
async def test_fund_query_handler_with_private_message(app: App):
    """测试私聊消息的处理"""
    from src.plugins.fund import fund_query
    from nonebot.adapters.onebot.v11 import PrivateMessageEvent

    expected_info_text = "测试基金信息"

    with patch("src.plugins.fund.query_by_code_type") as mock_query:
        mock_query.return_value = (expected_info_text, None)

        async with app.test_matcher(fund_query) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")

            # 创建私聊消息事件
            event = PrivateMessageEvent(
                time=int(datetime.now().timestamp()),
                self_id=987654321,
                post_type="message",
                sub_type="friend",
                user_id=123456,
                message_type="private",
                message_id=1,
                message=Message("002170"),
                original_message=Message("002170"),
                raw_message="002170",
                font=0,
                sender={
                    "user_id": 123456,
                    "nickname": "测试用户",
                    "sex": "unknown",
                    "age": 0,
                },
            )

            ctx.receive_event(bot, event)
            ctx.should_pass_rule()

            # 私聊应该调用 send_private_forward_msg
            ctx.should_call_api(
                "send_private_forward_msg",
                {
                    "user_id": 123456,
                    "messages": [
                        {
                            "type": "node",
                            "data": {"name": "", "uin": "987654321", "content": expected_info_text},
                        }
                    ],
                },
                result={"message_id": 999},
            )


@pytest.mark.asyncio
async def test_fund_query_multiple_codes(app: App):
    """测试多个不同类型的代码"""
    from src.plugins.fund import fund_query

    test_cases = [
        ("002170", "场外基金"),
        ("510300", "ETF"),
        ("000001.SZ", "股票"),
    ]

    for code, expected_type in test_cases:
        with patch("src.plugins.fund.query_by_code_type") as mock_query:
            mock_query.return_value = (f"{expected_type}信息", None)

            async with app.test_matcher(fund_query) as ctx:
                bot = ctx.create_bot(base=Bot, self_id="987654321")
                event = create_fake_group_message_event(code)

                ctx.receive_event(bot, event)
                ctx.should_pass_rule()

                ctx.should_call_api(
                    "send_group_forward_msg",
                    {
                        "group_id": 654321,
                        "messages": [
                            {
                                "type": "node",
                                "data": {
                                    "name": "",
                                    "uin": "987654321",
                                    "content": f"{expected_type}信息",
                                },
                            }
                        ],
                    },
                    result={"message_id": 999},
                )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
