"""
全局钩子函数 - 禁言状态检测与错误处理

提供双层保护机制：
1. run_preprocessor: 在Matcher执行前主动检测bot是否被禁言
2. run_postprocessor: 在Matcher执行后捕获发送消息失败的错误

这样可以在不修改任何插件代码的情况下，优雅地处理bot被禁言的情况。
"""

import time

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.exception import IgnoredException
from nonebot.matcher import Matcher
from nonebot.message import run_postprocessor, run_preprocessor

# 禁言状态缓存: {group_id: (check_timestamp, is_muted, mute_end_timestamp)}
# 缓存有效期 60 秒，避免每条消息都调用 API
_mute_cache: dict[int, tuple[float, bool, int]] = {}
_CACHE_TTL = 60.0  # 缓存有效期（秒）


@run_preprocessor
async def check_bot_mute_status(bot: Bot, event: MessageEvent, matcher: Matcher) -> None:
    """
    预处理钩子：检查bot是否被禁言

    在任何Matcher执行之前，检查bot在当前群组中是否被禁言。
    如果被禁言，则抛出IgnoredException跳过后续处理。

    使用内存缓存减少 API 调用频率，每群每 60 秒最多查询一次。

    Args:
        bot: Bot实例
        event: 群消息事件
        matcher: 当前Matcher

    Raises:
        IgnoredException: 当bot被禁言时，阻止Matcher继续执行
    """
    # 只检查群消息事件
    if not isinstance(event, GroupMessageEvent):
        return

    group_id = event.group_id
    current_time = time.time()

    # 检查缓存是否有效
    if group_id in _mute_cache:
        cache_time, is_muted, mute_end_timestamp = _mute_cache[group_id]

        # 缓存未过期
        if current_time - cache_time < _CACHE_TTL:
            if is_muted and mute_end_timestamp > current_time:
                remaining_minutes = int((mute_end_timestamp - current_time) // 60)
                logger.debug(
                    f"[缓存] 机器人在群 {group_id} 中被禁言，"
                    f"剩余 {remaining_minutes} 分钟，跳过消息处理"
                )
                raise IgnoredException("Bot is muted in this group (cached)")
            if not is_muted:
                # 缓存显示未禁言，直接放行
                return

    # 缓存过期或不存在，重新查询
    try:
        member_info = await bot.get_group_member_info(
            group_id=group_id,
            user_id=int(bot.self_id),
            no_cache=True
        )

        shut_up_timestamp = member_info.get("shut_up_timestamp", 0)
        is_muted = shut_up_timestamp > current_time

        # 更新缓存
        _mute_cache[group_id] = (current_time, is_muted, shut_up_timestamp)

        if is_muted:
            remaining_seconds = shut_up_timestamp - current_time
            remaining_minutes = int(remaining_seconds // 60)

            logger.warning(
                f"检测到机器人在群 {group_id} 中被禁言，"
                f"剩余 {remaining_minutes} 分钟，跳过消息处理"
            )
            raise IgnoredException("Bot is muted in this group")

    except ActionFailed as e:
        # API调用失败，记录警告但不阻止正常流程
        logger.warning(f"获取群成员信息失败 (群 {group_id}): {e}")
    except IgnoredException:
        # 重新抛出IgnoredException
        raise
    except Exception as e:
        # 其他异常，记录错误但不阻止正常流程
        logger.error(f"检查禁言状态时发生未预期的错误: {e}")


@run_postprocessor
async def handle_send_message_error(
    bot: Bot,
    event: MessageEvent,
    matcher: Matcher,
    exception: Exception | None,
) -> None:
    """
    后处理钩子：捕获发送消息失败的错误

    作为第二层保护，捕获Matcher执行过程中发生的ActionFailed错误。
    主要处理以下情况：
    - bot在preprocessor检查后、发送消息前被禁言
    - 其他权限相关的发送失败（retcode 1200, 120等）
    - preprocessor检查失败但bot实际被禁言的情况

    Args:
        bot: Bot实例
        event: 消息事件
        matcher: 当前Matcher
        exception: 捕获的异常（如果有）
    """
    # 只处理ActionFailed异常
    if not isinstance(exception, ActionFailed):
        return

    # 检查是否为禁言/权限相关错误
    # retcode 1200: 通用发送失败（通常是禁言）
    # retcode 120: 权限不足
    retcode = exception.retcode
    if retcode not in (1200, 120):
        return

    # 提取群组信息（如果是群消息）
    group_info = ""
    if isinstance(event, GroupMessageEvent):
        group_info = f"群 {event.group_id}"

    # 记录错误但不重新抛出，避免bot崩溃
    logger.warning(
        f"捕获到发送消息失败错误 (retcode={retcode}) "
        f"{group_info} - 可能是机器人被禁言或权限不足"
    )
    logger.debug(f"错误详情: {exception}")

    # 不重新抛出异常，让bot继续运行
    # 注意：这里我们选择"吞掉"异常，因为在禁言状态下无法发送任何消息
    # 包括错误提示消息也发送不出去
