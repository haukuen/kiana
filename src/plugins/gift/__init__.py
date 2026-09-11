"""随机礼物插件：随机挑一个可送的 QQ 礼物送给指定的人。"""

import time

from nonebot import get_plugin_config, logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.plugin import PluginMetadata
from nonebot_plugin_alconna import Alconna, Args, At, AtAll, Match, on_alconna

from ..group_permission import create_group_rule
from .config import Config
from .gifts import pick_gift
from .protocol import send_gift

__plugin_meta__ = PluginMetadata(
    name="gift",
    description="随机送出一个 QQ 礼物",
    usage=(
        "随机礼物 / 送礼物 - 随机挑一个可送的礼物送给自己\n"
        "送礼物@某人 - 送给被 @ 的人\n"
        "两者都以发命令的人的名义送出，命令本身不回消息"
    ),
    config=Config,
)

config: Config = get_plugin_config(Config)

_group_rule = create_group_rule(
    config_getter=lambda: config,
    plugin_enabled_attr="gift_plugin_enabled",
    prefix="gift_",
)

# 用 Alconna 而不是 on_fullmatch：at 段不计入纯文本，「送礼物 @某人」的纯文本是带尾空格的
# 「送礼物 」，而 FullmatchRule 是精确相等匹配，会漏掉带空格的写法。Alconna 直接吃 At/AtAll
# 组件，空格与否、@全体成员 的区分都由它处理。
gift_matcher = on_alconna(
    Alconna("送礼物", Args["target?", At | AtAll]),
    aliases={"随机礼物"},
    rule=_group_rule,
    priority=5,
    block=True,
)

# 冷却记录: {(group_id, 发命令的人): 上次成功时间}，group_id 为 0 表示私聊
_cooldowns: dict[tuple[int, int], float] = {}


def reset_state() -> None:
    """清空进程内状态（供测试与热重载使用）。"""
    _cooldowns.clear()


def _remaining_cooldown(key: tuple[int, int]) -> int:
    last_call = _cooldowns.get(key, 0.0)
    return max(0, int(last_call + config.gift_cooldown_time - time.time()))


def _display_name(event: MessageEvent) -> str:
    """发命令的人的显示名：群名片 > 昵称 > QQ 号。"""
    return event.sender.card or event.sender.nickname or str(event.user_id)


def _mentioned_qq(target: Match[At | AtAll]) -> int | None:
    """Alconna 解析出的收礼人。

    ``@全体成员`` 会落在 ``AtAll`` 分支，非 user 的 At（role/channel）target 也不是纯数字，
    两种都返回 None，由调用方退回发给自己。
    """
    if not target.available:
        return None
    value = target.result
    if isinstance(value, At) and value.target.isdigit():
        return int(value.target)
    return None


async def _member_name(bot: Bot, group_id: int, user_id: int) -> str:
    """被 @ 的人的显示名：群名片 > 昵称 > QQ 号。"""
    if group_id == 0:
        return str(user_id)
    try:
        info = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
    except Exception as e:
        logger.warning(f"[gift] 取成员 {user_id} 昵称失败: {e}")
        return str(user_id)
    return info.get("card") or info.get("nickname") or str(user_id)


async def _resolve_receiver(
    bot: Bot, event: MessageEvent, group_id: int, mentioned: int | None
) -> tuple[int, str]:
    """收礼人：被 @ 的人优先，没 @ 或 @ 的是自己就发给发命令的人。"""
    if mentioned is None or mentioned == event.user_id:
        return event.user_id, _display_name(event)
    return mentioned, await _member_name(bot, group_id, mentioned)


@gift_matcher.handle()
async def _handle_gift(bot: Bot, event: MessageEvent, target: Match[At | AtAll]) -> None:
    """命令全程静默：礼物本身在群里可见，不再额外发消息。

    冷却中、空池、发送失败都只记日志——对用户来说就是"什么都没发生"。
    """
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else 0
    # 冷却按发命令的人算，不按收礼人：要限制的是"谁能刷"（花的是 bot 账号的金币），
    # 否则同一个人换着人 @ 就能绕开冷却。
    cooldown_key = (group_id, event.user_id)

    remaining = _remaining_cooldown(cooldown_key)
    if remaining > 0:
        logger.debug(f"[gift] {cooldown_key} 冷却中，剩余 {remaining} 秒")
        return

    gift = pick_gift(config.gift_pool)
    if gift is None:
        logger.warning("[gift] 没有可送的礼物，检查 gift_pool 配置")
        return

    receiver_qq, receiver_nickname = await _resolve_receiver(
        bot, event, group_id, _mentioned_qq(target)
    )
    sender_nickname = _display_name(event)

    try:
        await send_gift(
            bot,
            gift=gift,
            group_id=group_id,
            target_qq=receiver_qq,
            target_nickname=receiver_nickname,
            # 以发命令的人的名义送：senduin/sendnickname 填他的，而不是 bot 自己的
            sender_qq=event.user_id,
            sender_nickname=sender_nickname,
        )
    except Exception as e:
        logger.error(f"[gift] 送礼失败: {e}", exc_info=True)
        return

    # 失败不计冷却，避免一次异常把用户锁在冷却里
    _cooldowns[cooldown_key] = time.time()
    logger.info(
        f"[gift] {sender_nickname}({event.user_id}) 向 {receiver_nickname}({receiver_qq}) "
        f"送出 {gift.name}（{gift.coin_value}金币）"
    )
