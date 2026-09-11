"""`MessageSvc.PbSendMsg` 礼物包的极简 Protobuf 编码。

移植自 bl-chat-plugin 的 sendGiftTool，字节布局与上游保持一致。只覆盖礼物包用到的
三种 wire 形态：varint、长度前缀的字符串、嵌套消息（空 dict 编码为零长度字段）。

外层信封的字段号取自 Msg 协议定义（`Msg`/`MessageBody`/`RichText`/`Elem`/`CommonElem`，
见 LLOneBot 的 `src/ntqqapi/proto/message.proto`），即：

    Message { routingHead = 1; contentHead = 2; body = 3 }
    MessageBody { richText = 1 }
    RichText { attr = 1; elems = 2 }
    Elem { lightApp = 51; commonElem = 53 }
    CommonElem { serviceType = 1; pbElem = 2; businessType = 3 }

礼物就是 `Elem.commonElem`（`serviceType = 41`）承载的一段业务体。业务体（`_gift_body`
里的 1~13 号字段）没有公开定义，是上游逆向出来的，字段含义按上游用法标注。
"""

from __future__ import annotations

import random
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot
from nonebot.adapters.onebot.v11.exception import ActionFailed

from .gifts import Gift

PB_CMD = "MessageSvc.PbSendMsg"
# OneBot 实现约定：retcode 1404 表示该 action 不存在。
UNSUPPORTED_ACTION_RETCODE = 1404
# CommonElem.serviceType：41 为礼物，33/37 为表情、45 为 Markdown、48 为图片/语音等。
GIFT_SERVICE_TYPE = 41

Message = dict[int, Any]


def _retcode(error: ActionFailed) -> Any:
    """ActionFailed 由适配器以 `ActionFailed(**响应体)` 抛出，retcode 在 info 里。"""
    return error.info.get("retcode") if isinstance(error.info, dict) else None


def _write_varint(out: bytearray, value: int) -> None:
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)


def _write_field(out: bytearray, tag: int, value: Any) -> None:
    if isinstance(value, dict):
        nested = encode_message(value)
        _write_varint(out, (tag << 3) | 2)
        _write_varint(out, len(nested))
        out += nested
    elif isinstance(value, str):
        payload = value.encode()
        _write_varint(out, (tag << 3) | 2)
        _write_varint(out, len(payload))
        out += payload
    elif isinstance(value, int):
        _write_varint(out, tag << 3)
        _write_varint(out, value)
    else:
        raise TypeError(f"不支持的 protobuf 字段类型: {type(value).__name__}")


def encode_message(message: Message) -> bytes:
    """按 tag 升序编码一条 protobuf 消息。"""
    out = bytearray()
    for tag in sorted(message):
        _write_field(out, tag, message[tag])
    return bytes(out)


def _gift_body(
    *,
    gift: Gift,
    target_qq: int,
    target_nickname: str,
    sender_qq: int,
    sender_nickname: str,
) -> Message:
    """礼物业务体，即 ``tencent.im.oidb.TroopGiftMsg.msg``。

    1~12 号字段号取自 QQ 客户端里的 pb 定义（``FieldMap`` 数组）：
    1 giftid / 2 giftname / 3 recvuin / 4 recvnickname / 5 senduin / 6 sendnickname /
    7 price / 8 orderid / 9 bgimage / 10 tianquanid / 11 level / 12 padding_top。
    上游省掉了 9（bgimage），并额外多带一个 13 号字段，后者不在该定义里。
    """
    return {
        1: 0,  # giftid：上游不填，只用 tianquanid 定位礼物
        2: gift.name,  # giftname：客户端按它展示礼物文案与图标
        3: target_qq,  # recvuin
        4: target_nickname,  # recvnickname
        5: sender_qq,  # senduin
        6: sender_nickname,  # sendnickname
        7: gift.display_value,  # price：展示价，与 13.4 一致
        8: {},  # orderid 空串；空串与空嵌套消息都编码成 0x42 0x00，不能省
        10: gift.tianquan_id,  # tianquanid：天权礼物 ID，真正决定送哪个礼物
        11: 5,  # level
        12: "30",  # padding_top
        13: {
            1: {2: gift.coin_value},  # 实扣金币
            2: 9,
            4: gift.display_value,
        },
    }


def build_gift_packet(
    *,
    gift: Gift,
    group_id: int,
    target_qq: int,
    target_nickname: str,
    sender_qq: int,
    sender_nickname: str,
) -> bytes:
    """构造完整的礼物数据包。`group_id` 为 0 表示私聊。"""
    packet: Message = {
        # Message.routingHead → RoutingHead.group → Group.groupCode
        1: {2: {1: group_id}},
        # Message.contentHead → ContentHead{msgType = 1, subType = 0, c2cCmd = 0}
        2: {1: 1, 2: 0, 3: 0},
        # Message.body → MessageBody.richText → RichText.elems → Elem.commonElem
        3: {
            1: {
                2: {
                    53: {
                        1: GIFT_SERVICE_TYPE,
                        2: _gift_body(
                            gift=gift,
                            target_qq=target_qq,
                            target_nickname=target_nickname,
                            sender_qq=sender_qq,
                            sender_nickname=sender_nickname,
                        ),
                    }
                }
            }
        },
        # 两个随机数，客户端/服务端各自用于去重
        4: random.getrandbits(32),
        5: random.getrandbits(32),
    }
    return encode_message(packet)


async def send_gift(
    bot: Bot,
    *,
    gift: Gift,
    group_id: int,
    target_qq: int,
    target_nickname: str,
    sender_qq: int,
    sender_nickname: str,
) -> None:
    """发送礼物包。

    优先走 NapCat 的 `send_packet`；仅当该 action 不存在（retcode 1404）时才退回
    LLBot 的 `send_pb`，避免真实发送失败时重复投递。
    """
    payload = build_gift_packet(
        gift=gift,
        group_id=group_id,
        target_qq=target_qq,
        target_nickname=target_nickname,
        sender_qq=sender_qq,
        sender_nickname=sender_nickname,
    ).hex()

    try:
        await bot.call_api("send_packet", cmd=PB_CMD, data=payload)
    except ActionFailed as e:
        if _retcode(e) != UNSUPPORTED_ACTION_RETCODE:
            raise
        logger.info("[gift] 当前 OneBot 实现不支持 send_packet，改用 send_pb")
        await bot.call_api("send_pb", cmd=PB_CMD, hex=payload)
