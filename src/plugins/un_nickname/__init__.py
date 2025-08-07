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
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as store

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="un_nickname",
    description="存储和管理群成员昵称",
    usage="@某人 昵称 xxx\n发送'at昵称'即可触发@",
    config=Config,
)

driver = get_driver()
superusers: set[str] = driver.config.superusers

config = get_plugin_config(Config)

# 获取存储文件路径
plugin_config_dir: Path = store.get_plugin_config_dir()
plugin_config_file: Path = store.get_config_file("un_nickname", "nicknames.json")

# 确保存储文件存在
if not plugin_config_file.exists():
    plugin_config_file.parent.mkdir(parents=True, exist_ok=True)
    with plugin_config_file.open("w", encoding="utf-8") as f:
        json.dump({}, f)


# 添加昵称处理函数
async def is_adding_nickname(event: GroupMessageEvent) -> bool:
    msg = event.message
    has_at = any(seg.type == "at" for seg in msg)
    text = msg.extract_plain_text().strip()
    return has_at and text.startswith("昵称")


add_nickname_matcher = on_message(rule=is_adding_nickname, priority=5, block=True)


# 添加昵称验证正则表达式
VALID_NICKNAME_PATTERN = re.compile(r"^[\u4e00-\u9fa5a-zA-Z0-9]+$")


# 昵称验证函数
def is_valid_nickname(nickname: str) -> bool:
    return bool(VALID_NICKNAME_PATTERN.match(nickname))


@add_nickname_matcher.handle()
async def handle_add_nickname(event: GroupMessageEvent):
    msg = event.message
    at_qq = None
    nickname = None
    # 提取被@的QQ号和昵称
    for seg in msg:
        if seg.type == "at":
            at_qq = seg.data.get("qq")
            break  # 只处理第一个@
    if not at_qq:
        return
    text = msg.extract_plain_text().strip()
    _, _, nickname_part = text.partition("昵称")
    if not nickname_part:
        return
    nickname = nickname_part.strip()
    if not nickname:
        await add_nickname_matcher.finish("昵称不能为空！")
        return
    if len(nickname) > config.max_nickname_length:
        await add_nickname_matcher.finish(f"昵称过长（最多{config.max_nickname_length}字符）")
        return
    if not is_valid_nickname(nickname):
        await add_nickname_matcher.finish("昵称只能包含汉字、字母和数字！")
        return

    # 读取并更新数据
    with plugin_config_file.open(encoding="utf-8") as f:
        data = json.load(f)
    group_id = str(event.group_id)
    if group_id not in data:
        data[group_id] = {}
    # 检查昵称是否已存在并提示
    existing_qq = data[group_id].get(nickname)
    if existing_qq and existing_qq != at_qq:
        await add_nickname_matcher.finish(f"昵称'{nickname}'已被其他用户占用！")
    else:
        data[group_id][nickname] = at_qq
        with plugin_config_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        await add_nickname_matcher.finish(f"昵称'{nickname}'成功绑定到用户！")


# 昵称替换处理函数
replace_nickname_matcher = on_message(priority=10, block=False)


@replace_nickname_matcher.handle()
async def handle_replace_nickname(bot: Bot, event: GroupMessageEvent):
    group_id = str(event.group_id)
    with plugin_config_file.open(encoding="utf-8") as f:
        data = json.load(f).get(group_id, {})

    nickname_to_qq = data
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

        for match in re.finditer(r"\bat(\S+)", text):
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


# 删除昵称处理函数
async def is_deleting_nickname(event: GroupMessageEvent) -> bool:
    msg = event.message
    text = msg.extract_plain_text().strip()
    return text.startswith(("删除昵称", "移除昵称")) and any(seg.type == "at" for seg in msg)


delete_nickname_matcher = on_message(rule=is_deleting_nickname, priority=5, block=True)


@delete_nickname_matcher.handle()
async def handle_delete_nickname(event: GroupMessageEvent, bot: Bot):
    msg = event.message
    text = msg.extract_plain_text().strip()

    # 分割命令和参数
    command_match = re.match(r"^(删除昵称|移除昵称)\s+(.+)$", text)
    if not command_match:
        await delete_nickname_matcher.finish("请指定要删除的昵称")
        return

    # 使用空格分割多个昵称
    nicknames = [n.strip() for n in command_match.group(2).split()]
    group_id = str(event.group_id)

    success = []
    not_found = []

    with plugin_config_file.open("r+", encoding="utf-8") as f:
        data = json.load(f)
        group_data = data.get(group_id, {})

        for nickname in nicknames:
            if nickname in group_data:
                del group_data[nickname]
                success.append(nickname)
            else:
                not_found.append(nickname)

        if success:  # 只有在有成功删除的情况下才写入文件
            data[group_id] = group_data
            f.seek(0)
            f.truncate()
            json.dump(data, f, ensure_ascii=False, indent=4)

    # 构建回复消息
    reply = []
    if success:
        reply.append(f"成功删除昵称：{' '.join(success)}")
    if not_found:
        reply.append(f"以下昵称不存在：{' '.join(not_found)}")

    reply_msg = "\n".join(reply) if reply else "未删除任何昵称"

    await delete_nickname_matcher.finish(reply_msg)


# 查询昵称处理函数
async def is_querying_nickname(event: GroupMessageEvent) -> bool:
    msg = event.message
    # 确保只有一个@且文本为"昵称"
    has_at = sum(seg.type == "at" for seg in msg) == 1
    text = msg.extract_plain_text().strip()
    return has_at and text == "昵称"


query_nickname_matcher = on_message(rule=is_querying_nickname, priority=5, block=True)


@query_nickname_matcher.handle()
async def handle_query_nickname(event: GroupMessageEvent):
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
    nicknames = [nick for nick, qq in data.items() if qq == at_qq]

    if not nicknames:
        await query_nickname_matcher.finish("该用户尚未设置任何昵称")
        return

    reply = f"当前用户的昵称：{', '.join(nicknames)}"
    await query_nickname_matcher.finish(reply)
