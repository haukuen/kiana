"""随机礼物插件：随机挑一个可送的 QQ 礼物送给命令触发者。"""

import time

from nonebot import get_plugin_config, logger, on_fullmatch
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.plugin import PluginMetadata

from ..group_permission import create_group_rule
from .config import Config
from .gifts import pick_gift
from .protocol import send_gift

__plugin_meta__ = PluginMetadata(
    name="gift",
    description="随机送出一个 QQ 礼物",
    usage="随机礼物 - 随机挑一个可送的礼物送给发命令的人（命令本身不回消息）",
    config=Config,
)

config: Config = get_plugin_config(Config)

gift_rule = create_group_rule(
    config_getter=lambda: config,
    plugin_enabled_attr="gift_plugin_enabled",
    prefix="gift_",
)

random_gift = on_fullmatch("随机礼物", rule=gift_rule, priority=5, block=True)

# 冷却记录: {(group_id, user_id): 上次成功时间}，group_id 为 0 表示私聊
_cooldowns: dict[tuple[int, int], float] = {}
# bot 昵称缓存: {self_id: nickname}
_bot_nicknames: dict[str, str] = {}


def reset_state() -> None:
    """清空进程内状态（供测试与热重载使用）。"""
    _cooldowns.clear()
    _bot_nicknames.clear()


def _remaining_cooldown(key: tuple[int, int]) -> int:
    last_call = _cooldowns.get(key, 0.0)
    return max(0, int(last_call + config.gift_cooldown_time - time.time()))


def _display_name(event: MessageEvent) -> str:
    return event.sender.card or event.sender.nickname or str(event.user_id)


async def _bot_nickname(bot: Bot) -> str:
    """取 bot 昵称，仅在成功时缓存。"""
    cached = _bot_nicknames.get(bot.self_id)
    if cached is not None:
        return cached

    nickname = ""
    try:
        info = await bot.get_login_info()
        nickname = str(info.get("nickname") or "").strip()
    except Exception as e:
        logger.warning(f"[gift] 获取 bot 昵称失败: {e}")

    if not nickname:
        return "机器人"

    _bot_nicknames[bot.self_id] = nickname
    return nickname


@random_gift.handle()
async def _handle_random_gift(bot: Bot, event: MessageEvent) -> None:
    """命令全程静默：礼物本身在群里可见，不再额外发消息。

    因此冷却中、空池、发送失败都只记日志——对用户来说就是"什么都没发生"。
    这条命令唯一的产物是那个礼物。
    """
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else 0
    cooldown_key = (group_id, event.user_id)

    remaining = _remaining_cooldown(cooldown_key)
    if remaining > 0:
        logger.debug(f"[gift] {cooldown_key} 冷却中，剩余 {remaining} 秒")
        return

    gift = pick_gift(config.gift_pool)
    if gift is None:
        logger.warning("[gift] 没有可送的礼物，检查 gift_pool 配置")
        return

    nickname = _display_name(event)

    try:
        await send_gift(
            bot,
            gift=gift,
            group_id=group_id,
            target_qq=event.user_id,
            target_nickname=nickname,
            sender_qq=int(bot.self_id),
            sender_nickname=await _bot_nickname(bot),
        )
    except Exception as e:
        logger.error(f"[gift] 送礼失败: {e}", exc_info=True)
        return

    # 失败不计冷却，避免一次异常把用户锁在冷却里
    _cooldowns[cooldown_key] = time.time()
    logger.info(f"[gift] 向 {nickname}({event.user_id}) 送出 {gift.name}（{gift.coin_value}金币）")
