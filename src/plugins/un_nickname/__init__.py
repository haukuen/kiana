# ruff: noqa: E402
import json
import re
from pathlib import Path

from nonebot import get_driver, get_plugin_config, on_message, require
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
)
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as store

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="un_nickname",
    description="存储和管理群成员昵称",
    usage="@某人 昵称 xxx\n发送'at昵称'即可触发@\n删除昵称 @某人\n清空昵称 @某人",
    config=Config,
)

driver = get_driver()
superusers: set[str] = driver.config.superusers

config = get_plugin_config(Config)

# 获取存储文件路径
plugin_config_dir: Path = store.get_plugin_config_dir()
plugin_config_file: Path = store.get_config_file("un_nickname", "nicknames.json")


def migrate_data_format() -> None:
    """迁移旧数据格式到新格式"""
    if not plugin_config_file.exists():
        plugin_config_file.parent.mkdir(parents=True, exist_ok=True)
        with plugin_config_file.open("w", encoding="utf-8") as f:
            json.dump({}, f)
        return

    with plugin_config_file.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # 检查是否需要迁移（检查第一个群的数据结构）
    if data:
        first_group_data = next(iter(data.values()))
        if first_group_data and isinstance(next(iter(first_group_data.values())), str):
            new_data = {}
            for group_id, group_data in data.items():
                new_data[group_id] = {}
                for nickname, user_id in group_data.items():
                    if user_id not in new_data[group_id]:
                        new_data[group_id][user_id] = []
                    new_data[group_id][user_id].append(nickname)

            # 保存迁移后的数据
            with plugin_config_file.open("w", encoding="utf-8") as f:
                json.dump(new_data, f, ensure_ascii=False, indent=4)


# 初始化时进行数据迁移
migrate_data_format()


# 添加昵称处理函数
async def is_adding_nickname(event: GroupMessageEvent) -> bool:
    msg = event.message
    has_at = any(seg.type == "at" for seg in msg)
    text = msg.extract_plain_text().strip()
    return has_at and text.startswith("昵称")


add_nickname_matcher = on_message(rule=is_adding_nickname, priority=5, block=True)


VALID_NICKNAME_PATTERN = re.compile(r"^[\u4e00-\u9fa5a-zA-Z0-9]+$")
AT_NICKNAME_PATTERN = re.compile(r"\bat\s*([\u4e00-\u9fa5a-zA-Z0-9]+)(?=\s|$)")


# 昵称验证函数
def is_valid_nickname(nickname: str) -> bool:
    return bool(VALID_NICKNAME_PATTERN.match(nickname))


def extract_at_qq_and_nickname(msg: Message) -> tuple[str | None, str | None]:
    """从消息中提取被@的QQ号和昵称"""
    at_qq = None
    # 提取被@的QQ号
    for seg in msg:
        if seg.type == "at":
            at_qq = seg.data.get("qq")
            break  # 只处理第一个@

    if not at_qq:
        return None, None

    text = msg.extract_plain_text().strip()
    _, _, nickname_part = text.partition("昵称")
    if not nickname_part:
        return at_qq, None

    nickname = nickname_part.strip()
    return at_qq, nickname


def validate_nickname(nickname: str) -> str | None:
    """验证昵称格式，返回错误信息或None"""
    if not nickname:
        return "昵称不能为空！"
    if len(nickname) > config.max_nickname_length:
        return f"昵称过长（最多{config.max_nickname_length}字符）"
    if not is_valid_nickname(nickname):
        return "昵称只能包含汉字、字母和数字！"
    return None


def check_nickname_occupied(data: dict, group_id: str, nickname: str, at_qq: str) -> bool:
    """检查昵称是否已被其他用户占用"""
    if group_id not in data:
        return False

    for user_id, nicknames in data[group_id].items():
        if nickname in nicknames and user_id != at_qq:
            return True
    return False


def add_nickname_to_data(data: dict, group_id: str, at_qq: str, nickname: str) -> bool:
    """将昵称添加到数据中，返回是否成功添加"""
    if group_id not in data:
        data[group_id] = {}

    if at_qq not in data[group_id]:
        data[group_id][at_qq] = []

    if nickname not in data[group_id][at_qq]:
        data[group_id][at_qq].append(nickname)
        return True
    return False


@add_nickname_matcher.handle()
async def handle_add_nickname(event: GroupMessageEvent):
    """处理添加昵称请求"""
    msg = event.message
    at_qq, nickname = extract_at_qq_and_nickname(msg)

    if not at_qq:
        return

    if not nickname:
        return

    # 验证昵称
    error_msg = validate_nickname(nickname)
    if error_msg:
        await add_nickname_matcher.finish(error_msg)
        return

    # 读取数据
    with plugin_config_file.open(encoding="utf-8") as f:
        data = json.load(f)

    group_id = str(event.group_id)

    # 检查昵称是否已被占用
    if check_nickname_occupied(data, group_id, nickname, at_qq):
        await add_nickname_matcher.finish(f"昵称'{nickname}'已被其他用户占用！")
        return

    # 添加昵称
    if add_nickname_to_data(data, group_id, at_qq, nickname):
        with plugin_config_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        await add_nickname_matcher.finish(f"昵称'{nickname}'成功绑定到用户！")
    else:
        await add_nickname_matcher.finish(f"用户已有昵称'{nickname}'！")


# 昵称替换处理函数
replace_nickname_matcher = on_message(priority=10, block=False)


@replace_nickname_matcher.handle()
async def handle_replace_nickname(bot: Bot, event: GroupMessageEvent):
    """处理昵称替换为@消息"""
    group_id = str(event.group_id)
    with plugin_config_file.open(encoding="utf-8") as f:
        data = json.load(f).get(group_id, {})

    # 构建昵称到用户ID的映射
    nickname_to_qq = {}
    for user_id, nicknames in data.items():
        for nickname in nicknames:
            nickname_to_qq[nickname] = user_id

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
            # 添加前面的文本段
            if start > last_pos:
                parts.append(MessageSegment.text(text[last_pos:start]))
            nickname = match.group(1)
            qq = nickname_to_qq.get(nickname)
            if qq:
                parts.append(MessageSegment.at(qq))
                replaced = True
            else:
                # 保留原始匹配文本
                parts.append(MessageSegment.text(match.group()))
            last_pos = end

        # 添加最后剩余的文本
        if last_pos < len(text):
            parts.append(MessageSegment.text(text[last_pos:]))

        # 将处理后的段落添加到新消息
        new_msg.extend(parts)

    if replaced:
        await bot.send(event, new_msg)


async def is_deleting_nickname(event: GroupMessageEvent) -> bool:
    """检查是否为删除昵称命令"""
    msg = event.message
    text = msg.extract_plain_text().strip()
    return text.startswith(("删除昵称", "移除昵称")) and any(seg.type == "at" for seg in msg)


async def is_clearing_nickname(event: GroupMessageEvent) -> bool:
    """检查是否为清空昵称命令"""
    msg = event.message
    text = msg.extract_plain_text().strip()
    return text.startswith(("清空昵称", "清除昵称")) and any(seg.type == "at" for seg in msg)


delete_nickname_matcher = on_message(rule=is_deleting_nickname, priority=5, block=True)
clear_nickname_matcher = on_message(
    rule=is_clearing_nickname, priority=5, block=True, permission=SUPERUSER
)


def extract_at_qq_from_message(msg: Message) -> str | None:
    """从消息中提取被@的QQ号"""
    for seg in msg:
        if seg.type == "at":
            return seg.data.get("qq")
    return None


def parse_delete_command(text: str) -> list[str] | None:
    """解析删除昵称命令，返回要删除的昵称列表"""
    command_match = re.match(r"^(删除昵称|移除昵称)\s+(.+)$", text)
    if not command_match:
        return None

    # 提取昵称部分（去除@信息）
    nickname_part = command_match.group(2).strip()
    # 移除可能的@信息
    nickname_part = re.sub(r"@\d+", "", nickname_part).strip()

    if not nickname_part:
        return None

    # 使用空格分割多个昵称
    return [n.strip() for n in nickname_part.split() if n.strip()]


def delete_nicknames_from_data(
    data: dict, group_id: str, at_qq: str, nicknames: list[str]
) -> tuple[list[str], list[str]]:
    """从数据中删除昵称，返回成功删除和未找到的昵称列表"""
    success = []
    not_found = []

    group_data = data.get(group_id, {})
    if at_qq not in group_data:
        return success, nicknames  # 所有昵称都未找到

    user_nicknames = group_data[at_qq]

    for nickname in nicknames:
        if nickname in user_nicknames:
            user_nicknames.remove(nickname)
            success.append(nickname)
        else:
            not_found.append(nickname)

    # 如果用户没有昵称了，删除该用户记录
    if not user_nicknames:
        del group_data[at_qq]

    return success, not_found


def build_delete_reply(success: list[str], not_found: list[str]) -> str:
    """构建删除昵称的回复消息"""
    reply = []
    if success:
        reply.append(f"成功删除昵称：{' '.join(success)}")
    if not_found:
        reply.append(f"以下昵称不存在：{' '.join(not_found)}")

    return "\n".join(reply) if reply else "未删除任何昵称"


@delete_nickname_matcher.handle()
async def handle_delete_nickname(event: GroupMessageEvent, bot: Bot):
    """处理删除特定昵称请求"""
    msg = event.message
    text = msg.extract_plain_text().strip()

    # 提取被@的QQ号
    at_qq = extract_at_qq_from_message(msg)
    if not at_qq:
        await delete_nickname_matcher.finish("请@要删除昵称的用户")
        return

    # 解析删除命令
    nicknames = parse_delete_command(text)
    if not nicknames:
        await delete_nickname_matcher.finish("请指定要删除的昵称")
        return

    group_id = str(event.group_id)

    # 读取和更新数据
    with plugin_config_file.open("r+", encoding="utf-8") as f:
        data = json.load(f)

        if group_id not in data or at_qq not in data[group_id]:
            await delete_nickname_matcher.finish("该用户没有任何昵称")
            return

        success, not_found = delete_nicknames_from_data(data, group_id, at_qq, nicknames)

        if success:  # 只有在有成功删除的情况下才写入文件
            f.seek(0)
            f.truncate()
            json.dump(data, f, ensure_ascii=False, indent=4)

    # 发送回复
    reply_msg = build_delete_reply(success, not_found)
    await delete_nickname_matcher.finish(reply_msg)


@clear_nickname_matcher.handle()
async def handle_clear_nickname(event: GroupMessageEvent, bot: Bot):
    """处理清空用户所有昵称请求"""
    msg = event.message
    at_qq = None

    # 提取被@的QQ号
    for seg in msg:
        if seg.type == "at":
            at_qq = seg.data.get("qq")
            break

    if not at_qq:
        await clear_nickname_matcher.finish("请@要清空昵称的用户")
        return

    group_id = str(event.group_id)

    with plugin_config_file.open("r+", encoding="utf-8") as f:
        data = json.load(f)
        group_data = data.get(group_id, {})

        if at_qq not in group_data:
            await clear_nickname_matcher.finish("该用户没有任何昵称")
            return

        cleared_nicknames = group_data[at_qq].copy()
        del group_data[at_qq]

        data[group_id] = group_data
        f.seek(0)
        f.truncate()
        json.dump(data, f, ensure_ascii=False, indent=4)

    await clear_nickname_matcher.finish(f"已清空该用户的所有昵称：{', '.join(cleared_nicknames)}")


# 查询昵称处理函数
async def is_querying_nickname(event: GroupMessageEvent) -> bool:
    """检查是否为查询昵称命令"""
    msg = event.message
    # 确保只有一个@且文本为"昵称"
    has_at = sum(seg.type == "at" for seg in msg) == 1
    text = msg.extract_plain_text().strip()
    return has_at and text == "昵称"


query_nickname_matcher = on_message(rule=is_querying_nickname, priority=5, block=True)


@query_nickname_matcher.handle()
async def handle_query_nickname(event: GroupMessageEvent):
    """处理查询用户昵称请求"""
    msg = event.message
    at_qq = None

    # 提取被@的QQ号
    for seg in msg:
        if seg.type == "at":
            at_qq = seg.data.get("qq")
            break

    if not at_qq:
        return

    # 读取数据
    group_id = str(event.group_id)
    with plugin_config_file.open(encoding="utf-8") as f:
        data = json.load(f).get(group_id, {})

    # 查找该用户的所有昵称
    nicknames = data.get(at_qq, [])

    if not nicknames:
        await query_nickname_matcher.finish("该用户尚未设置任何昵称")
        return

    reply = f"当前用户的昵称：{', '.join(nicknames)}"
    await query_nickname_matcher.finish(reply)
