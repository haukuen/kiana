from nonebot import get_plugin_config, on_message, require
from nonebot.plugin import PluginMetadata
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment, GroupMessageEvent, Bot, Message
from nonebot.params import EventMessage
from pathlib import Path
import json
import re

require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as store

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="un_nickname",
    description="存储和管理群成员昵称",
    usage="@某人 昵称 xxx\n发送'at昵称'即可触发@",
    config=Config,
)

config = get_plugin_config(Config)

# 获取存储文件路径
plugin_config_dir: Path = store.get_plugin_config_dir()
plugin_config_file: Path = store.get_config_file("un_nickname", "nicknames.json")

# 确保存储文件存在
if not plugin_config_file.exists():
    plugin_config_file.parent.mkdir(parents=True, exist_ok=True)
    with open(plugin_config_file, 'w', encoding='utf-8') as f:
        json.dump({}, f)

# 添加昵称处理函数
async def is_adding_nickname(event: GroupMessageEvent) -> bool:
    msg = event.message
    has_at = any(seg.type == 'at' for seg in msg)
    text = msg.extract_plain_text().strip()
    return has_at and text.startswith("昵称")

add_nickname_matcher = on_message(rule=is_adding_nickname, priority=5, block=True)

@add_nickname_matcher.handle()
async def handle_add_nickname(event: GroupMessageEvent):
    msg = event.message
    at_qq = None
    nickname = None
    # 提取被@的QQ号和昵称
    for seg in msg:
        if seg.type == 'at':
            at_qq = seg.data.get('qq')
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
    if len(nickname) > 15:
        await add_nickname_matcher.finish("昵称过长（最多15字符）")
        return
    
    # 读取并更新数据
    with open(plugin_config_file, 'r', encoding='utf-8') as f:
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
        with open(plugin_config_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        await add_nickname_matcher.finish(f"昵称'{nickname}'成功绑定到用户！")

# 昵称替换处理函数
replace_nickname_matcher = on_message(priority=10, block=False)

@replace_nickname_matcher.handle()
async def handle_replace_nickname(bot: Bot, event: GroupMessageEvent):
    group_id = str(event.group_id)
    with open(plugin_config_file, 'r', encoding='utf-8') as f:
        data = json.load(f).get(group_id, {})
    
    nickname_to_qq = data
    original_msg = event.message
    new_msg = Message()
    replaced = False

    for seg in original_msg:
        if seg.type != 'text':
            new_msg.append(seg)
            continue
        
        text = seg.data['text']
        parts = []
        last_pos = 0
        
        for match in re.finditer(r'\bat(\S+)', text):
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