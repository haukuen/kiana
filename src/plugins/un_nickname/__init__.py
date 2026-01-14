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
    description="存储和管理群成员昵称和集合",
    usage="@某人 昵称 xxx\n发送'at昵称'即可触发@\n删除昵称 @某人\n清空昵称 @某人\n集合 xxx @人 创建/添加成员\n集合 xxx 查看成员\n集合列表\n移除集合 xxx @人\n删除集合 xxx",
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
        """
        CREATE TABLE IF NOT EXISTS nickname_collections (
            group_id TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            user_id TEXT NOT NULL,
            PRIMARY KEY (group_id, collection_name, user_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_collections_group_name
        ON nickname_collections (group_id, collection_name)
        """,
    ]
)

# 昵称映射缓存: {group_id: (expires_at, {nickname: user_id})}
_nickname_cache: dict[str, tuple[float, dict[str, str]]] = {}
# 集合缓存: {group_id: (expires_at, {collection_name: list[user_id]})}
_collection_cache: dict[str, tuple[float, dict[str, list[str]]]] = {}
# 分群锁，避免跨群阻塞
# 注意：锁一旦创建就不会被删除，因为清理锁会引入复杂的竞态条件问题。
# 锁对象非常轻量，且实际使用中群组数量通常是有限的，内存开销可忽略不计。
_cache_locks: dict[str, asyncio.Lock] = {}
# 全局锁，用于保护 _cache_locks 字典的创建操作，避免竞态条件
_global_lock = asyncio.Lock()
CACHE_TTL = 300
EMPTY_CACHE_TTL = 30


async def _get_group_lock(group_id: str) -> asyncio.Lock:
    """获取指定群的缓存锁。

    使用全局锁保护锁的创建，避免竞态条件下为同一 group_id 创建多个锁实例。
    锁一旦创建就会永久保留，不会被清理。
    """
    lock = _cache_locks.get(group_id)
    if lock is not None:
        return lock

    async with _global_lock:
        # 双重检查，避免重复创建
        lock = _cache_locks.get(group_id)
        if lock is None:
            lock = asyncio.Lock()
            _cache_locks[group_id] = lock
        return lock


async def _invalidate_cache(group_id: str) -> None:
    """清除指定群组的昵称映射缓存

    注意：不能在获取锁之前检查缓存是否存在并提前返回，因为这会与并发的缓存写入
    产生竞态条件。必须始终获取锁，确保检查/删除相对于缓存写入操作保持原子性。
    """
    lock = await _get_group_lock(group_id)
    async with lock:
        if group_id in _nickname_cache:
            del _nickname_cache[group_id]
            logger.debug(f"已清除群组 {group_id} 的昵称缓存")
        if group_id in _collection_cache:
            del _collection_cache[group_id]
            logger.debug(f"已清除群组 {group_id} 的集合缓存")


async def _get_cached_nickname_map(group_id: str) -> dict[str, str]:
    """获取群组的昵称映射（带缓存）"""
    # 快速路径：无锁检查缓存是否有效
    cached = _nickname_cache.get(group_id)
    if cached and time() < cached[0]:
        logger.debug(f"使用群组 {group_id} 的昵称缓存")
        return cached[1]

    lock = await _get_group_lock(group_id)
    async with lock:
        # 双重检查，避免重复回源；重新获取时间戳避免跨 await 的过期判断误差
        cached = _nickname_cache.get(group_id)
        if cached and time() < cached[0]:
            logger.debug(f"使用群组 {group_id} 的昵称缓存（锁内）")
            return cached[1]

        logger.debug(f"从数据库查询群组 {group_id} 的昵称映射")
        group_data = await fetch_group_nickname_map(group_id)

        # 将 {user_id: [nicknames]} 转换为 {nickname: user_id}
        nickname_to_qq: dict[str, str] = {}
        for user_id, nicknames in group_data.items():
            for nickname in nicknames:
                nickname_to_qq[nickname] = user_id

        ttl = CACHE_TTL if nickname_to_qq else EMPTY_CACHE_TTL
        # DB 查询后使用新的时间戳计算过期时间，确保 TTL 准确
        _nickname_cache[group_id] = (time() + ttl, nickname_to_qq)
        logger.debug(f"已缓存群组 {group_id} 的 {len(nickname_to_qq)} 个昵称映射，TTL={ttl}s")

        return nickname_to_qq


async def _get_cached_collection_map(group_id: str) -> dict[str, list[str]]:
    """获取群组的集合映射（带缓存）"""
    cached = _collection_cache.get(group_id)
    if cached and time() < cached[0]:
        logger.debug(f"使用群组 {group_id} 的集合缓存")
        return cached[1]

    lock = await _get_group_lock(group_id)
    async with lock:
        cached = _collection_cache.get(group_id)
        if cached and time() < cached[0]:
            logger.debug(f"使用群组 {group_id} 的集合缓存（锁内）")
            return cached[1]

        logger.debug(f"从数据库查询群组 {group_id} 的集合映射")
        collection_map = await fetch_all_collections_map(group_id)

        ttl = CACHE_TTL if collection_map else EMPTY_CACHE_TTL
        _collection_cache[group_id] = (time() + ttl, collection_map)
        logger.debug(f"已缓存群组 {group_id} 的 {len(collection_map)} 个集合映射，TTL={ttl}s")

        return collection_map


def is_adding_nickname(event: GroupMessageEvent) -> bool:
    msg = event.message
    has_at = any(seg.type == "at" for seg in msg)
    text = msg.extract_plain_text().strip()
    return has_at and text.startswith("昵称")


def is_replacing_nickname(event: GroupMessageEvent) -> bool:
    """检查消息是否包含 'at' 关键字"""
    text = event.message.extract_plain_text()
    return "at" in text


add_nickname_matcher = on_message(rule=is_adding_nickname, priority=5, block=True)


VALID_NICKNAME_PATTERN = re.compile(r"^[\u4e00-\u9fa5a-zA-Z0-9]+$")
AT_NICKNAME_PATTERN = re.compile(r"\bat\s*([\u4e00-\u9fa5a-zA-Z0-9]+)(?=\s|$)")


def is_valid_nickname(nickname: str) -> bool:
    return bool(VALID_NICKNAME_PATTERN.match(nickname))


def extract_at_qq_from_message(msg: Message) -> str | None:
    """从消息中提取第一个 @目标的 QQ 号"""
    return next((seg.data.get("qq") for seg in msg if seg.type == "at"), None)


def extract_at_qq_and_nickname(msg: Message) -> tuple[str | None, str | None]:
    at_qq = extract_at_qq_from_message(msg)

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


def validate_collection_name(name: str) -> str | None:
    if not name:
        return "集合名不能为空！"
    if len(name) > config.max_collection_name_length:
        return f"集合名过长（最多{config.max_collection_name_length}字符）"
    if not is_valid_nickname(name):
        return "集合名只能包含汉字、字母和数字！"
    return None


async def nickname_occupied(group_id: str, nickname: str, user_id: str) -> str | None:
    """检查昵称是否被其他用户占用,返回占用者的 user_id,如果未被占用则返回 None"""
    row = await db.fetch_one(
        """
        SELECT user_id
        FROM nicknames
        WHERE group_id = ? AND nickname = ? AND user_id <> ?
        LIMIT 1
        """,
        (group_id, nickname, user_id),
    )
    return row["user_id"] if row else None


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


async def name_exists_as_nickname(group_id: str, name: str) -> bool:
    """检查名称是否已被昵称占用"""
    row = await db.fetch_one(
        "SELECT 1 FROM nicknames WHERE group_id = ? AND nickname = ? LIMIT 1",
        (group_id, name),
    )
    return row is not None


async def name_exists_as_collection(group_id: str, name: str) -> bool:
    """检查名称是否已被集合占用"""
    row = await db.fetch_one(
        "SELECT 1 FROM nickname_collections WHERE group_id = ? AND collection_name = ? LIMIT 1",
        (group_id, name),
    )
    return row is not None


async def fetch_all_collections_map(group_id: str) -> dict[str, list[str]]:
    """获取群组的所有集合映射 {collection_name: [user_ids]}"""
    rows = await db.fetch_all(
        """
        SELECT collection_name, user_id
        FROM nickname_collections
        WHERE group_id = ?
        ORDER BY collection_name
        """,
        (group_id,),
    )
    mapping: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        mapping[row["collection_name"]].append(row["user_id"])
    return mapping


async def fetch_collection_members(group_id: str, collection_name: str) -> list[str]:
    """获取指定集合的成员列表"""
    rows = await db.fetch_all(
        """
        SELECT user_id
        FROM nickname_collections
        WHERE group_id = ? AND collection_name = ?
        """,
        (group_id, collection_name),
    )
    return [row["user_id"] for row in rows]


async def fetch_all_collections_summary(group_id: str) -> list[tuple[str, int]]:
    """获取群组所有集合的名称和成员数"""
    rows = await db.fetch_all(
        """
        SELECT collection_name, COUNT(*) as member_count
        FROM nickname_collections
        WHERE group_id = ?
        GROUP BY collection_name
        ORDER BY collection_name
        """,
        (group_id,),
    )
    return [(row["collection_name"], row["member_count"]) for row in rows]


async def add_collection_members(
    group_id: str, collection_name: str, user_ids: list[str]
) -> tuple[list[str], list[str]]:
    """向集合添加成员，返回 (新增成功列表, 已存在列表)"""
    added: list[str] = []
    already_exists: list[str] = []

    for user_id in user_ids:
        try:
            await db.execute(
                """
                INSERT INTO nickname_collections (group_id, collection_name, user_id)
                VALUES (?, ?, ?)
                """,
                (group_id, collection_name, user_id),
            )
            added.append(user_id)
        except sqlite3.IntegrityError:
            already_exists.append(user_id)

    if added:
        await _invalidate_cache(group_id)

    return added, already_exists


async def remove_collection_members(
    group_id: str, collection_name: str, user_ids: list[str]
) -> tuple[list[str], list[str], bool]:
    """从集合移除成员，返回 (成功移除列表, 不存在列表, 集合是否已删除)"""
    removed: list[str] = []
    not_found: list[str] = []

    for user_id in user_ids:
        existing = await db.fetch_one(
            """
            SELECT 1 FROM nickname_collections
            WHERE group_id = ? AND collection_name = ? AND user_id = ?
            LIMIT 1
            """,
            (group_id, collection_name, user_id),
        )
        if existing:
            await db.execute(
                """
                DELETE FROM nickname_collections
                WHERE group_id = ? AND collection_name = ? AND user_id = ?
                """,
                (group_id, collection_name, user_id),
            )
            removed.append(user_id)
        else:
            not_found.append(user_id)

    collection_deleted = False
    if removed:
        remaining = await db.fetch_one(
            """
            SELECT 1 FROM nickname_collections
            WHERE group_id = ? AND collection_name = ?
            LIMIT 1
            """,
            (group_id, collection_name),
        )
        if not remaining:
            collection_deleted = True
        await _invalidate_cache(group_id)

    return removed, not_found, collection_deleted


async def delete_collection(group_id: str, collection_name: str) -> list[str]:
    """删除整个集合，返回被删除的成员列表"""
    members = await fetch_collection_members(group_id, collection_name)
    if not members:
        return []

    await db.execute(
        """
        DELETE FROM nickname_collections
        WHERE group_id = ? AND collection_name = ?
        """,
        (group_id, collection_name),
    )
    await _invalidate_cache(group_id)
    return members


async def remove_user_from_all_collections(group_id: str, user_id: str) -> list[str]:
    """从群组所有集合中移除用户，返回受影响的集合名列表，并自动删除空集合"""
    rows = await db.fetch_all(
        """
        SELECT collection_name
        FROM nickname_collections
        WHERE group_id = ? AND user_id = ?
        """,
        (group_id, user_id),
    )
    affected_collections = [row["collection_name"] for row in rows]

    if not affected_collections:
        return []

    await db.execute(
        """
        DELETE FROM nickname_collections
        WHERE group_id = ? AND user_id = ?
        """,
        (group_id, user_id),
    )

    deleted_collections: list[str] = []
    for collection_name in affected_collections:
        remaining = await db.fetch_one(
            """
            SELECT 1 FROM nickname_collections
            WHERE group_id = ? AND collection_name = ?
            LIMIT 1
            """,
            (group_id, collection_name),
        )
        if not remaining:
            deleted_collections.append(collection_name)

    if affected_collections:
        await _invalidate_cache(group_id)

    return deleted_collections


@add_nickname_matcher.handle()
async def handle_add_nickname(bot: Bot, event: GroupMessageEvent) -> None:
    msg = event.message
    at_qq, nickname = extract_at_qq_and_nickname(msg)

    if not at_qq:
        return

    if not nickname:
        existing = await fetch_user_nicknames(str(event.group_id), at_qq)
        if existing:
            await add_nickname_matcher.finish("该用户的昵称:" + ", ".join(existing))
        else:
            await add_nickname_matcher.finish("该用户没有任何昵称")
        return

    error_msg = validate_nickname(nickname)
    if error_msg:
        await add_nickname_matcher.finish(error_msg)
        return

    group_id = str(event.group_id)

    if await name_exists_as_collection(group_id, nickname):
        await add_nickname_matcher.finish(f"名称「{nickname}」已被集合占用!")
        return

    occupied_user_id = await nickname_occupied(group_id, nickname, at_qq)
    if occupied_user_id:
        try:
            member_info = await bot.get_group_member_info(
                group_id=int(group_id), user_id=int(occupied_user_id)
            )
            occupied_user_name = (
                member_info.get("card") or member_info.get("nickname") or occupied_user_id
            )
        except Exception as e:
            logger.warning(f"获取用户 {occupied_user_id} 信息失败: {e}")
            occupied_user_name = occupied_user_id

        await add_nickname_matcher.finish(f"昵称'{nickname}'已被 {occupied_user_name} 占用!")
        return

    if await add_nickname_record(group_id, at_qq, nickname):
        await add_nickname_matcher.finish(f"昵称'{nickname}'成功绑定到用户!")
    else:
        await add_nickname_matcher.finish(f"用户已有昵称'{nickname}'!")


replace_nickname_matcher = on_message(rule=is_replacing_nickname, priority=10, block=False)


def _resolve_at_target(
    name: str,
    sender_id: str,
    nickname_to_qq: dict[str, str],
    collection_to_users: dict[str, list[str]],
) -> list[MessageSegment] | None:
    """解析 at 目标，返回消息段列表或 None（未找到）"""
    qq = nickname_to_qq.get(name)
    if qq:
        return [MessageSegment.at(qq)]

    members = collection_to_users.get(name)
    if members:
        filtered = [uid for uid in members if uid != sender_id]
        if filtered:
            return [MessageSegment.at(uid) for uid in filtered]

    return None


@replace_nickname_matcher.handle()
async def handle_replace_nickname(bot: Bot, event: GroupMessageEvent) -> None:
    """处理昵称替换，将 'at昵称' 替换为实际的 @mentions"""
    group_id = str(event.group_id)
    sender_id = str(event.user_id)
    nickname_to_qq = await _get_cached_nickname_map(group_id)
    collection_to_users = await _get_cached_collection_map(group_id)

    original_msg = event.message
    new_msg = Message()
    replaced = False

    for seg in original_msg:
        if seg.type != "text":
            new_msg.append(seg)
            continue

        text = seg.data["text"]
        parts: list[MessageSegment] = []
        last_pos = 0

        for match in AT_NICKNAME_PATTERN.finditer(text):
            start, end = match.span()
            if start > last_pos:
                parts.append(MessageSegment.text(text[last_pos:start]))

            name = match.group(1)
            at_segments = _resolve_at_target(name, sender_id, nickname_to_qq, collection_to_users)
            if at_segments:
                parts.extend(at_segments)
                replaced = True
            else:
                parts.append(MessageSegment.text(match.group()))
            last_pos = end

        if last_pos < len(text):
            parts.append(MessageSegment.text(text[last_pos:]))

        new_msg.extend(parts)

    if replaced:
        await bot.send(event, new_msg)


def is_deleting_nickname(event: GroupMessageEvent) -> bool:
    msg = event.message
    text = msg.extract_plain_text().strip()
    return text.startswith(("删除昵称", "移除昵称")) and any(seg.type == "at" for seg in msg)


def is_clearing_nickname(event: GroupMessageEvent) -> bool:
    msg = event.message
    text = msg.extract_plain_text().strip()
    return text.startswith(("清空昵称", "清除昵称")) and any(seg.type == "at" for seg in msg)


delete_nickname_matcher = on_message(rule=is_deleting_nickname, priority=5, block=True)
clear_nickname_matcher = on_message(
    rule=is_clearing_nickname, priority=5, block=True, permission=SUPERUSER
)


def parse_delete_command(text: str) -> list[str] | None:
    command_match = re.match(r"^(删除昵称|移除昵称)\s+(.+)$", text)
    if not command_match:
        return None

    nickname_part = command_match.group(2).strip()
    nickname_part = re.sub(r"@\d+", "", nickname_part).strip()

    if not nickname_part:
        return None

    return [n.strip() for n in nickname_part.split() if n.strip()]


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    """去重并保持原有顺序"""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


async def delete_nicknames_from_data(
    group_id: str, at_qq: str, nicknames: list[str]
) -> tuple[list[str], list[str]]:
    """批量删除昵称，返回 (成功列表, 不存在列表)"""
    if not nicknames:
        return [], []

    # 去重并保持原有顺序，避免冗余 SQL 和重复结果
    unique_nicknames = _dedupe_preserve_order(nicknames)

    placeholders = ",".join("?" * len(unique_nicknames))
    rows = await db.fetch_all(
        f"SELECT nickname FROM nicknames "  # noqa: S608
        f"WHERE group_id = ? AND user_id = ? AND nickname IN ({placeholders})",
        (group_id, at_qq, *unique_nicknames),
    )
    existing_set = {row["nickname"] for row in rows}

    success = [n for n in unique_nicknames if n in existing_set]
    not_found = [n for n in unique_nicknames if n not in existing_set]

    if success:
        delete_placeholders = ",".join("?" * len(success))
        await db.execute(
            f"DELETE FROM nicknames "  # noqa: S608
            f"WHERE group_id = ? AND user_id = ? AND nickname IN ({delete_placeholders})",
            (group_id, at_qq, *success),
        )
        await _invalidate_cache(group_id)

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
    at_qq = extract_at_qq_from_message(event.message)

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

    deleted_collections = await remove_user_from_all_collections(group_id, user_id)
    if deleted_collections:
        logger.info(
            f"用户 {user_id} 退出群 {group_id}，已从集合中移除，"
            f"以下空集合已自动删除: {', '.join(deleted_collections)}"
        )


async def is_group_admin_or_superuser(bot: Bot, event: GroupMessageEvent) -> bool:
    """检查用户是否为群管理员、群主或超级用户"""
    if await SUPERUSER(bot, event):
        return True
    try:
        member_info = await bot.get_group_member_info(
            group_id=event.group_id, user_id=event.user_id
        )
        return member_info.get("role") in ("admin", "owner")
    except Exception:
        return False


def is_managing_collection(event: GroupMessageEvent) -> bool:
    """检查是否为集合管理命令: 集合 xxx @人"""
    text = event.message.extract_plain_text().strip()
    has_at = any(seg.type == "at" for seg in event.message)
    return text.startswith("集合 ") and has_at


def is_viewing_collection(event: GroupMessageEvent) -> bool:
    """检查是否为查看集合命令: 集合 xxx (无@)"""
    text = event.message.extract_plain_text().strip()
    has_at = any(seg.type == "at" for seg in event.message)
    return text.startswith("集合 ") and not has_at and text != "集合列表"


def is_listing_collections(event: GroupMessageEvent) -> bool:
    """检查是否为列出集合命令"""
    text = event.message.extract_plain_text().strip()
    return text == "集合列表"


def is_removing_from_collection(event: GroupMessageEvent) -> bool:
    """检查是否为移除集合成员命令"""
    text = event.message.extract_plain_text().strip()
    has_at = any(seg.type == "at" for seg in event.message)
    return text.startswith("移除集合 ") and has_at


def is_deleting_collection(event: GroupMessageEvent) -> bool:
    """检查是否为删除集合命令"""
    text = event.message.extract_plain_text().strip()
    return text.startswith("删除集合 ")


manage_collection_matcher = on_message(rule=is_managing_collection, priority=5, block=True)
view_collection_matcher = on_message(rule=is_viewing_collection, priority=5, block=True)
list_collections_matcher = on_message(rule=is_listing_collections, priority=5, block=True)
remove_from_collection_matcher = on_message(
    rule=is_removing_from_collection, priority=5, block=True
)
delete_collection_matcher = on_message(rule=is_deleting_collection, priority=5, block=True)


def extract_all_at_qq(msg: Message) -> list[str]:
    """从消息中提取所有 @目标的 QQ 号"""
    result = [str(seg.data.get("qq")) for seg in msg if seg.type == "at" and seg.data.get("qq")]
    return list(dict.fromkeys(result))


def parse_collection_name_from_command(text: str, prefix: str) -> str | None:
    """从命令中提取集合名"""
    if not text.startswith(prefix):
        return None
    rest = text[len(prefix) :].strip()
    parts = rest.split()
    return parts[0] if parts else None


async def get_member_names(bot: Bot, group_id: int, user_ids: list[str]) -> dict[str, str]:
    """批量获取成员昵称"""
    names: dict[str, str] = {}
    for uid in user_ids:
        try:
            info = await bot.get_group_member_info(group_id=group_id, user_id=int(uid))
            names[uid] = info.get("card") or info.get("nickname") or uid
        except Exception:
            names[uid] = uid
    return names


@manage_collection_matcher.handle()
async def handle_manage_collection(bot: Bot, event: GroupMessageEvent) -> None:
    if not await is_group_admin_or_superuser(bot, event):
        await manage_collection_matcher.finish("仅管理员可以管理集合")
        return

    text = event.message.extract_plain_text().strip()
    collection_name = parse_collection_name_from_command(text, "集合 ")

    if not collection_name:
        await manage_collection_matcher.finish("请指定集合名")
        return

    error_msg = validate_collection_name(collection_name)
    if error_msg:
        await manage_collection_matcher.finish(error_msg)
        return

    group_id = str(event.group_id)
    user_ids = extract_all_at_qq(event.message)

    if not user_ids:
        await manage_collection_matcher.finish("请@要添加到集合的成员")
        return

    if await name_exists_as_nickname(group_id, collection_name):
        await manage_collection_matcher.finish(f"名称「{collection_name}」已被昵称占用!")
        return

    existing_members = await fetch_collection_members(group_id, collection_name)
    is_new_collection = len(existing_members) == 0
    max_members = config.max_collection_members

    existing_set = set(existing_members)
    new_user_count = sum(1 for uid in user_ids if uid not in existing_set)

    if len(existing_members) + new_user_count > max_members:
        await manage_collection_matcher.finish(f"集合成员数超过上限（最多{max_members}人）")
        return

    added, already_exists = await add_collection_members(group_id, collection_name, user_ids)

    if not added:
        await manage_collection_matcher.finish(f"这些成员已在集合「{collection_name}」中")
        return

    member_names = await get_member_names(bot, event.group_id, added)
    names_str = "、".join(member_names.values())

    if is_new_collection:
        reply = f"已创建集合「{collection_name}」，添加了 {len(added)} 人: {names_str}"
    else:
        reply = f"已向集合「{collection_name}」添加 {len(added)} 人: {names_str}"

    if already_exists:
        reply += f"\n（{len(already_exists)} 人已在集合中）"

    await manage_collection_matcher.finish(reply)


@view_collection_matcher.handle()
async def handle_view_collection(bot: Bot, event: GroupMessageEvent) -> None:
    text = event.message.extract_plain_text().strip()
    collection_name = parse_collection_name_from_command(text, "集合 ")

    if not collection_name:
        await view_collection_matcher.finish("请指定集合名")
        return

    group_id = str(event.group_id)
    members = await fetch_collection_members(group_id, collection_name)

    if not members:
        await view_collection_matcher.finish(f"集合「{collection_name}」不存在")
        return

    member_names = await get_member_names(bot, event.group_id, members)
    names_str = "、".join(member_names.values())
    await view_collection_matcher.finish(
        f"集合「{collection_name}」共 {len(members)} 人: {names_str}"
    )


@list_collections_matcher.handle()
async def handle_list_collections(bot: Bot, event: GroupMessageEvent) -> None:
    group_id = str(event.group_id)
    collections = await fetch_all_collections_summary(group_id)

    if not collections:
        await list_collections_matcher.finish("本群暂无集合")
        return

    lines = [f"本群共 {len(collections)} 个集合:"]
    for name, count in collections:
        lines.append(f"  • {name} ({count}人)")

    await list_collections_matcher.finish("\n".join(lines))


@remove_from_collection_matcher.handle()
async def handle_remove_from_collection(bot: Bot, event: GroupMessageEvent) -> None:
    if not await is_group_admin_or_superuser(bot, event):
        await remove_from_collection_matcher.finish("仅管理员可以管理集合")
        return

    text = event.message.extract_plain_text().strip()
    collection_name = parse_collection_name_from_command(text, "移除集合 ")

    if not collection_name:
        await remove_from_collection_matcher.finish("请指定集合名")
        return

    group_id = str(event.group_id)
    user_ids = extract_all_at_qq(event.message)

    if not user_ids:
        await remove_from_collection_matcher.finish("请@要移除的成员")
        return

    existing_members = await fetch_collection_members(group_id, collection_name)
    if not existing_members:
        await remove_from_collection_matcher.finish(f"集合「{collection_name}」不存在")
        return

    removed, not_found, collection_deleted = await remove_collection_members(
        group_id, collection_name, user_ids
    )

    if removed:
        member_names = await get_member_names(bot, event.group_id, removed)
        names_str = "、".join(member_names.values())
        reply = f"已从集合「{collection_name}」移除: {names_str}"
        if collection_deleted:
            reply += f"\n集合「{collection_name}」已无成员，已自动删除"
        if not_found:
            reply += f"\n（{len(not_found)} 人不在集合中）"
        await remove_from_collection_matcher.finish(reply)
    else:
        await remove_from_collection_matcher.finish("这些成员不在集合中")


@delete_collection_matcher.handle()
async def handle_delete_collection(bot: Bot, event: GroupMessageEvent) -> None:
    if not await is_group_admin_or_superuser(bot, event):
        await delete_collection_matcher.finish("仅管理员可以删除集合")
        return

    text = event.message.extract_plain_text().strip()
    collection_name = parse_collection_name_from_command(text, "删除集合 ")

    if not collection_name:
        await delete_collection_matcher.finish("请指定集合名")
        return

    group_id = str(event.group_id)
    deleted_members = await delete_collection(group_id, collection_name)

    if deleted_members:
        await delete_collection_matcher.finish(
            f"已删除集合「{collection_name}」（原有 {len(deleted_members)} 人）"
        )
    else:
        await delete_collection_matcher.finish(f"集合「{collection_name}」不存在")
