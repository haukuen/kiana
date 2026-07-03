import re

from nonebot import get_plugin_config, logger, on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupDecreaseNoticeEvent,
    GroupMessageEvent,
    Message,
    MessageSegment,
)

from src.plugins.forward_utils import send_forward_message

from .cache import get_cached_collection_map, get_cached_nickname_map
from .config import Config
from .db import (
    _add_to_existing_collection,
    add_collection_members,
    add_nickname_record,
    clear_user_nicknames,
    delete_collection,
    delete_nicknames_from_data,
    fetch_all_collections_summary,
    fetch_collection_members,
    fetch_user_nicknames,
    name_exists_as_collection,
    name_exists_as_nickname,
    nickname_occupied,
    remove_collection_members,
    remove_user_from_all_collections,
    upgrade_nickname_to_collection,
)
from .utils import (
    AT_NICKNAME_PATTERN,
    build_delete_reply,
    extract_all_at_qq,
    extract_at_qq_and_nickname,
    extract_at_qq_from_message,
    get_member_names,
    parse_collection_name_from_command,
    parse_delete_command,
    validate_collection_name,
    validate_nickname,
)

config: Config = get_plugin_config(Config)


# ============== Rule 函数 ==============


def is_adding_nickname(event: GroupMessageEvent) -> bool:
    """检查是否为添加昵称命令"""
    msg = event.message
    has_at = any(seg.type == "at" for seg in msg)
    text = msg.extract_plain_text().strip()
    return has_at and text.startswith("昵称")


def is_replacing_nickname(event: GroupMessageEvent) -> bool:
    """检查消息是否包含 'at昵称' 触发语法"""
    text = event.message.extract_plain_text()
    return bool(AT_NICKNAME_PATTERN.search(text))


def is_deleting_nickname(event: GroupMessageEvent) -> bool:
    """检查是否为删除昵称命令"""
    msg = event.message
    text = msg.extract_plain_text().strip()
    return text.startswith(("删除昵称", "移除昵称")) and any(seg.type == "at" for seg in msg)


def is_clearing_nickname(event: GroupMessageEvent) -> bool:
    """检查是否为清空昵称命令"""
    msg = event.message
    text = msg.extract_plain_text().strip()
    return text.startswith(("清空昵称", "清除昵称")) and any(seg.type == "at" for seg in msg)


def is_group_decrease_event(event: Event) -> bool:
    """检查是否为群成员减少事件"""
    return isinstance(event, GroupDecreaseNoticeEvent)


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


# ============== Matchers ==============

add_nickname_matcher = on_message(rule=is_adding_nickname, priority=5, block=True)
replace_nickname_matcher = on_message(rule=is_replacing_nickname, priority=10, block=False)
delete_nickname_matcher = on_message(rule=is_deleting_nickname, priority=5, block=True)
clear_nickname_matcher = on_message(rule=is_clearing_nickname, priority=5, block=True)
group_decrease_matcher = on_notice(rule=is_group_decrease_event, priority=50, block=False)
manage_collection_matcher = on_message(rule=is_managing_collection, priority=5, block=True)
view_collection_matcher = on_message(rule=is_viewing_collection, priority=5, block=True)
list_collections_matcher = on_message(rule=is_listing_collections, priority=5, block=True)
remove_from_collection_matcher = on_message(
    rule=is_removing_from_collection, priority=5, block=True
)
delete_collection_matcher = on_message(rule=is_deleting_collection, priority=5, block=True)


# ============== 辅助函数 ==============


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


# ============== Handlers ==============


async def _send_nickname_list(
    bot: Bot, event: GroupMessageEvent, nicknames: list[str], prefix: str, forward_title: str
) -> None:
    """发送昵称列表，过多时使用合并转发避免刷屏"""
    text = ", ".join(nicknames)
    if len(text) > 200:
        node = {
            "type": "node",
            "data": {"name": forward_title, "uin": bot.self_id, "content": text},
        }
        await send_forward_message(bot, event, [node])
    else:
        await add_nickname_matcher.finish(f"{prefix}{text}")


@add_nickname_matcher.handle()
async def handle_add_nickname(bot: Bot, event: GroupMessageEvent) -> None:
    msg = event.message
    at_qq, nickname = extract_at_qq_and_nickname(msg)

    if not at_qq:
        return

    if not nickname:
        existing = await fetch_user_nicknames(str(event.group_id), at_qq)
        if existing:
            await _send_nickname_list(bot, event, existing, "该用户的昵称:", "昵称列表")
        else:
            await add_nickname_matcher.finish("该用户没有任何昵称")
        return

    error_msg = validate_nickname(nickname)
    if error_msg:
        await add_nickname_matcher.finish(error_msg)
        return

    group_id = str(event.group_id)

    if await name_exists_as_collection(group_id, nickname):
        success, message = await _add_to_existing_collection(group_id, nickname, at_qq)
        await add_nickname_matcher.finish(message)
        return

    occupied_user_id = await nickname_occupied(group_id, nickname, at_qq)
    if occupied_user_id:
        success, error_msg = await upgrade_nickname_to_collection(
            group_id, nickname, at_qq, occupied_user_id
        )
        if success:
            msg = "已升级为集合"
            try:
                member_info = await bot.get_group_member_info(
                    group_id=int(group_id), user_id=int(occupied_user_id)
                )
                occupied_user_name = (
                    member_info.get("card") or member_info.get("nickname") or occupied_user_id
                )
                msg = f"已升级为集合，还有一个是{occupied_user_name}"
            except Exception as e:
                logger.warning(f"获取用户 {occupied_user_id} 信息失败: {e}")
            await add_nickname_matcher.finish(msg)
        else:
            await add_nickname_matcher.finish(error_msg)
        return

    if await add_nickname_record(group_id, at_qq, nickname):
        await add_nickname_matcher.finish(f"昵称'{nickname}'成功绑定到用户!")
    else:
        await add_nickname_matcher.finish(f"用户已有昵称'{nickname}'!")


def _replace_text_segment(
    text: str,
    matches: list[re.Match[str]],
    sender_id: str,
    nickname_to_qq: dict[str, str],
    collection_to_users: dict[str, list[str]],
) -> tuple[list[MessageSegment], bool]:
    """替换单个 text 段中的 at 昵称，返回（消息段列表, 是否发生替换）"""
    parts: list[MessageSegment] = []
    replaced = False
    last_pos = 0

    for match in matches:
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

    return parts, replaced


@replace_nickname_matcher.handle()
async def handle_replace_nickname(bot: Bot, event: GroupMessageEvent) -> None:
    """处理昵称替换，将 'at昵称' 替换为实际的 @mentions"""
    group_id = str(event.group_id)
    sender_id = str(event.user_id)

    # 先扫描所有 text 段收集候选名，无命中则直接返回，避免无谓的缓存/DB 查询
    matches_by_text: dict[str, list[re.Match[str]]] = {}
    for seg in event.message:
        if seg.type != "text":
            continue
        text = seg.data["text"]
        if text not in matches_by_text:
            ms = list(AT_NICKNAME_PATTERN.finditer(text))
            if ms:
                matches_by_text[text] = ms
    if not matches_by_text:
        return

    nickname_to_qq = await get_cached_nickname_map(group_id)
    collection_to_users = await get_cached_collection_map(group_id)

    new_msg = Message()
    replaced = False

    for seg in event.message:
        if seg.type != "text":
            new_msg.append(seg)
            continue

        matches = matches_by_text.get(seg.data["text"])
        if not matches:
            new_msg.append(seg)
            continue

        parts, did_replace = _replace_text_segment(
            seg.data["text"], matches, sender_id, nickname_to_qq, collection_to_users
        )
        new_msg.extend(parts)
        replaced = replaced or did_replace

    if replaced:
        await bot.send(event, new_msg)


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

    await _send_nickname_list(bot, event, cleared_nicknames, "已清空该用户的所有昵称：", "清空昵称")


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


@manage_collection_matcher.handle()
async def handle_manage_collection(bot: Bot, event: GroupMessageEvent) -> None:
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
