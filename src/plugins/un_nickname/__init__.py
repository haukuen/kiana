import asyncio
import re
import sqlite3
from collections import defaultdict
from time import time

from nonebot import get_plugin_config, logger, on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupDecreaseNoticeEvent,
    GroupMessageEvent,
    Message,
    MessageSegment,
)
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

from src.storage import get_db

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="un_nickname",
    description="存储和管理群成员昵称",
    usage="@某人 昵称 xxx\n发送'at昵称'即可触发@\n删除昵称 @某人\n清空昵称 @某人",
    config=Config,
)

config = get_plugin_config(Config)

db = get_db()
db.ensure_schema(
    [
        """
        CREATE TABLE IF NOT EXISTS nicknames (
            group_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            nickname TEXT NOT NULL,
            PRIMARY KEY (group_id, user_id, nickname)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_nicknames_group_nickname
        ON nicknames (group_id, nickname)
        """,
    ]
)

# 昵称映射缓存: {group_id: (timestamp, {nickname: user_id})}
_nickname_cache: dict[str, tuple[float, dict[str, str]]] = {}
_cache_lock = asyncio.Lock()
CACHE_TTL = 300


async def _invalidate_cache(group_id: str) -> None:
    """清除指定群组的昵称映射缓存"""
    async with _cache_lock:
        if group_id in _nickname_cache:
            del _nickname_cache[group_id]
            logger.debug(f"已清除群组 {group_id} 的昵称缓存")


async def _get_cached_nickname_map(group_id: str) -> dict[str, str]:
    """获取群组的昵称映射（带缓存）"""
    async with _cache_lock:
        now = time()

        # 检查缓存是否存在且未过期
        if group_id in _nickname_cache:
            cached_time, cached_data = _nickname_cache[group_id]
            if now - cached_time < CACHE_TTL:
                logger.debug(f"使用群组 {group_id} 的昵称缓存")
                return cached_data

        # 从数据库查询
        logger.debug(f"从数据库查询群组 {group_id} 的昵称映射")
        group_data = await fetch_group_nickname_map(group_id)

        # 将 {user_id: [nicknames]} 转换为 {nickname: user_id}
        nickname_to_qq: dict[str, str] = {}
        for user_id, nicknames in group_data.items():
            for nickname in nicknames:
                nickname_to_qq[nickname] = user_id

        _nickname_cache[group_id] = (now, nickname_to_qq)
        logger.debug(f"已缓存群组 {group_id} 的 {len(nickname_to_qq)} 个昵称映射")

        return nickname_to_qq


async def is_adding_nickname(event: GroupMessageEvent) -> bool:
    msg = event.message
    has_at = any(seg.type == "at" for seg in msg)
    text = msg.extract_plain_text().strip()
    return has_at and text.startswith("昵称")


async def is_replacing_nickname(event: GroupMessageEvent) -> bool:
    """检查消息是否包含 'at' 关键字"""
    text = event.message.extract_plain_text()
    return "at" in text


add_nickname_matcher = on_message(rule=is_adding_nickname, priority=5, block=True)


VALID_NICKNAME_PATTERN = re.compile(r"^[\u4e00-\u9fa5a-zA-Z0-9]+$")
AT_NICKNAME_PATTERN = re.compile(r"\bat\s*([\u4e00-\u9fa5a-zA-Z0-9]+)(?=\s|$)")


def is_valid_nickname(nickname: str) -> bool:
    return bool(VALID_NICKNAME_PATTERN.match(nickname))


def extract_at_qq_and_nickname(msg: Message) -> tuple[str | None, str | None]:
    at_qq = None
    for seg in msg:
        if seg.type == "at":
            at_qq = seg.data.get("qq")
            break

    if not at_qq:
        return None, None

    text = msg.extract_plain_text().strip()
    _, _, nickname_part = text.partition("昵称")
    if not nickname_part:
        return at_qq, None

    nickname = nickname_part.strip()
    return at_qq, nickname


def validate_nickname(nickname: str) -> str | None:
    if not nickname:
        return "昵称不能为空！"
    if len(nickname) > config.max_nickname_length:
        return f"昵称过长（最多{config.max_nickname_length}字符）"
    if not is_valid_nickname(nickname):
        return "昵称只能包含汉字、字母和数字！"
    return None


async def nickname_occupied(group_id: str, nickname: str, user_id: str) -> bool:
    row = await db.fetch_one(
        """
        SELECT user_id
        FROM nicknames
        WHERE group_id = ? AND nickname = ? AND user_id <> ?
        LIMIT 1
        """,
        (group_id, nickname, user_id),
    )
    return row is not None


async def add_nickname_record(group_id: str, user_id: str, nickname: str) -> bool:
    """添加昵称记录，返回是否成功（False 表示已存在）"""
    try:
        await db.execute(
            """
            INSERT INTO nicknames (group_id, user_id, nickname)
            VALUES (?, ?, ?)
            """,
            (group_id, user_id, nickname),
        )
        await _invalidate_cache(group_id)
        return True
    except sqlite3.IntegrityError:
        return False


async def fetch_group_nickname_map(group_id: str) -> dict[str, list[str]]:
    rows = await db.fetch_all(
        """
        SELECT user_id, nickname
        FROM nicknames
        WHERE group_id = ?
        """,
        (group_id,),
    )
    mapping: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        mapping[row["user_id"]].append(row["nickname"])
    return mapping


async def fetch_user_nicknames(group_id: str, user_id: str) -> list[str]:
    rows = await db.fetch_all(
        """
        SELECT nickname
        FROM nicknames
        WHERE group_id = ? AND user_id = ?
        ORDER BY nickname
        """,
        (group_id, user_id),
    )
    return [row["nickname"] for row in rows]


async def delete_single_nickname(group_id: str, user_id: str, nickname: str) -> bool:
    """删除单个昵称，返回是否成功"""
    existing = await db.fetch_one(
        """
        SELECT 1 FROM nicknames
        WHERE group_id = ? AND user_id = ? AND nickname = ?
        LIMIT 1
        """,
        (group_id, user_id, nickname),
    )
    if not existing:
        return False

    await db.execute(
        """
        DELETE FROM nicknames
        WHERE group_id = ? AND user_id = ? AND nickname = ?
        """,
        (group_id, user_id, nickname),
    )
    await _invalidate_cache(group_id)
    return True


async def clear_user_nicknames(group_id: str, user_id: str) -> list[str]:
    """清空用户的所有昵称，返回被清空的昵称列表"""
    nicknames = await fetch_user_nicknames(group_id, user_id)
    if not nicknames:
        return []

    await db.execute(
        """
        DELETE FROM nicknames
        WHERE group_id = ? AND user_id = ?
        """,
        (group_id, user_id),
    )
    await _invalidate_cache(group_id)
    return nicknames


@add_nickname_matcher.handle()
async def handle_add_nickname(bot: Bot, event: GroupMessageEvent) -> None:
    msg = event.message
    at_qq, nickname = extract_at_qq_and_nickname(msg)

    if not at_qq:
        return

    if not nickname:
        existing = await fetch_user_nicknames(str(event.group_id), at_qq)
        if existing:
            await add_nickname_matcher.finish("该用户的昵称：" + ", ".join(existing))
        else:
            await add_nickname_matcher.finish("该用户没有任何昵称")
        return

    error_msg = validate_nickname(nickname)
    if error_msg:
        await add_nickname_matcher.finish(error_msg)
        return

    group_id = str(event.group_id)

    if await nickname_occupied(group_id, nickname, at_qq):
        await add_nickname_matcher.finish(f"昵称'{nickname}'已被其他用户占用！")
        return

    if await add_nickname_record(group_id, at_qq, nickname):
        await add_nickname_matcher.finish(f"昵称'{nickname}'成功绑定到用户！")
    else:
        await add_nickname_matcher.finish(f"用户已有昵称'{nickname}'！")


replace_nickname_matcher = on_message(rule=is_replacing_nickname, priority=10, block=False)


@replace_nickname_matcher.handle()
async def handle_replace_nickname(bot: Bot, event: GroupMessageEvent) -> None:
    """处理昵称替换，将 'at昵称' 替换为实际的 @mentions"""
    group_id = str(event.group_id)
    nickname_to_qq = await _get_cached_nickname_map(group_id)

    original_msg = event.message
    new_msg = Message()
    replaced = False

    for seg in original_msg:
        if seg.type != "text":
            new_msg.append(seg)
            continue

        text = seg.data["text"]
        parts = []
        last_pos = 0

        for match in AT_NICKNAME_PATTERN.finditer(text):
            start, end = match.span()
            if start > last_pos:
                parts.append(MessageSegment.text(text[last_pos:start]))
            nickname = match.group(1)
            qq = nickname_to_qq.get(nickname)
            if qq:
                parts.append(MessageSegment.at(qq))
                replaced = True
            else:
                parts.append(MessageSegment.text(match.group()))
            last_pos = end

        if last_pos < len(text):
            parts.append(MessageSegment.text(text[last_pos:]))

        new_msg.extend(parts)

    if replaced:
        await bot.send(event, new_msg)


async def is_deleting_nickname(event: GroupMessageEvent) -> bool:
    msg = event.message
    text = msg.extract_plain_text().strip()
    return text.startswith(("删除昵称", "移除昵称")) and any(seg.type == "at" for seg in msg)


async def is_clearing_nickname(event: GroupMessageEvent) -> bool:
    msg = event.message
    text = msg.extract_plain_text().strip()
    return text.startswith(("清空昵称", "清除昵称")) and any(seg.type == "at" for seg in msg)


delete_nickname_matcher = on_message(rule=is_deleting_nickname, priority=5, block=True)
clear_nickname_matcher = on_message(
    rule=is_clearing_nickname, priority=5, block=True, permission=SUPERUSER
)


def extract_at_qq_from_message(msg: Message) -> str | None:
    for seg in msg:
        if seg.type == "at":
            return seg.data.get("qq")
    return None


def parse_delete_command(text: str) -> list[str] | None:
    command_match = re.match(r"^(删除昵称|移除昵称)\s+(.+)$", text)
    if not command_match:
        return None

    nickname_part = command_match.group(2).strip()
    nickname_part = re.sub(r"@\d+", "", nickname_part).strip()

    if not nickname_part:
        return None

    return [n.strip() for n in nickname_part.split() if n.strip()]


async def delete_nicknames_from_data(
    group_id: str, at_qq: str, nicknames: list[str]
) -> tuple[list[str], list[str]]:
    success: list[str] = []
    not_found: list[str] = []

    for nickname in nicknames:
        if await delete_single_nickname(group_id, at_qq, nickname):
            success.append(nickname)
        else:
            not_found.append(nickname)

    return success, not_found


def build_delete_reply(success: list[str], not_found: list[str]) -> str:
    reply = []
    if success:
        reply.append(f"成功删除昵称：{' '.join(success)}")
    if not_found:
        reply.append(f"以下昵称不存在：{' '.join(not_found)}")

    return "\n".join(reply) if reply else "未删除任何昵称"


@delete_nickname_matcher.handle()
async def handle_delete_nickname(bot: Bot, event: GroupMessageEvent) -> None:
    msg = event.message
    text = msg.extract_plain_text().strip()

    at_qq = extract_at_qq_from_message(msg)
    if not at_qq:
        await delete_nickname_matcher.finish("请@要删除昵称的用户")
        return

    nicknames = parse_delete_command(text)
    if not nicknames:
        await delete_nickname_matcher.finish("请指定要删除的昵称")
        return

    group_id = str(event.group_id)

    user_nicknames = await fetch_user_nicknames(group_id, at_qq)
    if not user_nicknames:
        await delete_nickname_matcher.finish("该用户没有任何昵称")
        return

    success, not_found = await delete_nicknames_from_data(group_id, at_qq, nicknames)

    reply_msg = build_delete_reply(success, not_found)
    await delete_nickname_matcher.finish(reply_msg)


@clear_nickname_matcher.handle()
async def handle_clear_nickname(bot: Bot, event: GroupMessageEvent) -> None:
    msg = event.message
    at_qq = None

    for seg in msg:
        if seg.type == "at":
            at_qq = seg.data.get("qq")
            break

    if not at_qq:
        await clear_nickname_matcher.finish("请@要清空昵称的用户")
        return

    group_id = str(event.group_id)

    cleared_nicknames = await clear_user_nicknames(group_id, at_qq)
    if not cleared_nicknames:
        await clear_nickname_matcher.finish("该用户没有任何昵称")
        return

    await clear_nickname_matcher.finish(f"已清空该用户的所有昵称：{', '.join(cleared_nicknames)}")


def is_group_decrease_event(event: Event) -> bool:
    """检查是否为群成员减少事件"""
    return isinstance(event, GroupDecreaseNoticeEvent)


group_decrease_matcher = on_notice(rule=is_group_decrease_event, priority=50, block=False)


@group_decrease_matcher.handle()
async def handle_group_decrease(bot: Bot, event: GroupDecreaseNoticeEvent) -> None:
    """监听群成员减少事件，自动清理该用户的昵称"""
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    bot_id = str(bot.self_id)

    # 跳过机器人自身的退群事件
    if user_id == bot_id:
        logger.debug(f"机器人自身退出群 {group_id}，跳过昵称清理")
        return

    cleared_nicknames = await clear_user_nicknames(group_id, user_id)
    if cleared_nicknames:
        logger.info(
            f"用户 {user_id} 退出群 {group_id}，已自动清理其昵称: {', '.join(cleared_nicknames)}"
        )
