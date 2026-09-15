"""炼化相关测试共用的事件、API 预期和响应工厂。"""

from datetime import datetime

import httpx2
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender


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


def expect_bot_not_muted(
    ctx, group_id: int = 200001, self_id: int = 987654321
) -> None:
    """在发送消息预期前声明群禁言查询，供全局预处理器消费。"""
    ctx.should_call_api(
        "get_group_member_info",
        {"group_id": group_id, "user_id": self_id, "no_cache": True},
        result={"shut_up_timestamp": 0},
    )


def fake_ai_response(content: str = "AI 生成的总结") -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "choices": [
                {"message": {"role": "assistant", "content": content}},
            ],
        },
        request=httpx2.Request("POST", "https://example.com/v1/chat/completions"),
    )


def fake_dt():
    """返回 strftime 始终输出 T 的假时间对象。"""
    return type("FakeDT", (), {"strftime": lambda self, fmt: "T"})()
