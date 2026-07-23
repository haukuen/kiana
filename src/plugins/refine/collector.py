"""发言采集器：从 message_archive 取目标发言，拼装成 AI prompt。

依赖 ``message_archive`` 插件的 DB API（``fetch_group_messages_by_time_range``）。
message_archive 表未初始化时直接返回空（容错）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nonebot import logger

from .db import RefineSubscription, TargetType

if TYPE_CHECKING:
    from .config import Config


@dataclass(slots=True)
class CollectedMessages:
    """采集结果。``messages`` 已截断到 max_messages。"""

    messages: list[tuple[int, str, str]]  # (event_time, sender_name, plain_text)
    period_start: int
    period_end: int


async def _fetch_un_nickname_collection_members(
    group_id: str, collection_name: str
) -> list[str]:
    """懒加载避免循环 import。返回空列表表示集合不存在或 un_nickname 未加载。"""
    try:
        from src.plugins.un_nickname.db import (  # noqa: PLC0415
            fetch_collection_members,
        )
    except ImportError:
        logger.warning("[炼化] un_nickname 插件未加载，无法解析 collection 订阅")
        return []
    return await fetch_collection_members(group_id, collection_name)


async def collect_messages_for_subscription(
    sub: RefineSubscription,
    *,
    lookback_hours: int,
    max_messages: int,
    max_prompt_chars: int,  # 保留参数以保持调用方对称
    period_end: int | None = None,
) -> CollectedMessages:
    """采集一个订阅在回看窗口内的发言。"""
    end = period_end if period_end is not None else int(time.time())
    start = end - lookback_hours * 3600

    if sub.target_type == "user":
        user_ids: list[str] = [sub.target_value]
    else:
        user_ids = await _fetch_un_nickname_collection_members(
            sub.group_id, sub.target_value
        )
        if not user_ids:
            return CollectedMessages(messages=[], period_start=start, period_end=end)

    try:
        from src.plugins.message_archive.db import (  # noqa: PLC0415
            fetch_group_messages_by_time_range,
        )

        archived = await fetch_group_messages_by_time_range(
            group_id=sub.group_id,
            start_time=start,
            end_time=end,
        )
    except Exception as e:
        logger.warning(f"[炼化] 读取 message_archive 失败: {e}")
        return CollectedMessages(messages=[], period_start=start, period_end=end)

    target_set = set(user_ids)
    picked: list[tuple[int, str, str]] = []
    for msg in archived:
        if msg.user_id not in target_set:
            continue
        text = msg.plain_text.strip()
        if not text:
            continue
        picked.append((msg.event_time, msg.sender_name, text))
        if len(picked) >= max_messages:
            break

    return CollectedMessages(messages=picked, period_start=start, period_end=end)


def build_prompt_payload(
    collected: CollectedMessages, *, max_prompt_chars: int
) -> str:
    """把采集到的消息渲染成 prompt 原文段落。

    每条格式: ``[MM-DD HH:MM] <昵称>: <文本>``，整体截断到 max_prompt_chars。
    """
    if not collected.messages:
        return ""
    lines: list[str] = []
    total = 0
    for event_time, name, text in collected.messages:
        local = time.strftime("%m-%d %H:%M", time.localtime(event_time))
        line = f"[{local}] {name}: {text}"
        if total + len(line) + 1 > max_prompt_chars:
            remaining = max_prompt_chars - total
            if remaining <= 0:
                break
            line = line[:remaining]
            lines.append(line)
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


async def collect_and_build_payload(
    sub: RefineSubscription,
    config: Config,
) -> tuple[CollectedMessages, str]:
    """一站式：采集 + 拼 prompt。返回 (collected, prompt_payload)。

    prompt_payload 为空字符串表示目标在回看窗口内没有任何发言（跳过 AI 调用）。
    """
    collected = await collect_messages_for_subscription(
        sub,
        lookback_hours=config.refine_lookback_hours,
        max_messages=config.refine_max_messages_per_target,
        max_prompt_chars=config.refine_max_prompt_chars,
    )
    if not collected.messages:
        return collected, ""
    payload = build_prompt_payload(
        collected, max_prompt_chars=config.refine_max_prompt_chars
    )
    return collected, payload


def _parse_prefix_value(text: str) -> tuple[TargetType, str] | tuple[None, None]:
    """处理 ``prefix:value`` 形式。"""
    prefix, _, rest = text.partition(":")
    prefix = prefix.strip().lower()
    rest = rest.strip()
    if not rest:
        return None, None
    if prefix in {"collection", "集合"}:
        return "collection", rest
    if prefix == "user":
        return "user", rest
    return None, None


def resolve_target_type_and_value(
    raw_arg: str,
) -> tuple[TargetType, str] | tuple[None, None]:
    """解析用户输入的订阅目标。

    支持两种形式:
        collection:<名>           -> ('collection', '<名>')
        集合 <名> / 集合<名>      -> ('collection', '<名>')  (中文别名)
        user:<qq>                 -> ('user', '<qq>')
        纯数字                    -> ('user', '<qq>')

    返回 (None, None) 表示格式不识别。
    """
    text = raw_arg.strip()
    if not text:
        return None, None

    if ":" in text:
        return _parse_prefix_value(text)

    if text.startswith("集合"):
        rest = text[2:].strip()
        if rest:
            return "collection", rest
        return None, None

    if text.isdigit():
        return "user", text

    return None, None
