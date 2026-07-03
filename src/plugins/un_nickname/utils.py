import re

from nonebot import get_plugin_config
from nonebot.adapters.onebot.v11 import Bot, Message

from .config import Config

config: Config = get_plugin_config(Config)

# 正则表达式常量
VALID_NICKNAME_PATTERN = re.compile(r"^[\u4e00-\u9fa5a-zA-Z0-9]+$")
# 捕获 at 昵称：'at' 后可有一个空格，名字由汉字/字母/数字组成，
# 名字后不能再跟名字字符（避免贪婪吞掉后续中文，也兼容中文标点/句尾）。
AT_NICKNAME_PATTERN = re.compile(r"\bat ?([\u4e00-\u9fa5a-zA-Z0-9]+)(?![\u4e00-\u9fa5a-zA-Z0-9])")


def is_valid_nickname(nickname: str) -> bool:
    """检查昵称格式是否有效"""
    return bool(VALID_NICKNAME_PATTERN.match(nickname))


def validate_nickname(nickname: str) -> str | None:
    """验证昵称，返回错误消息或 None（表示验证通过）"""
    if not nickname:
        return "昵称不能为空！"
    if len(nickname) > config.max_nickname_length:
        return f"昵称过长（最多{config.max_nickname_length}字符）"
    if not is_valid_nickname(nickname):
        return "昵称只能包含汉字、字母和数字！"
    return None


def validate_collection_name(name: str) -> str | None:
    """验证集合名，返回错误消息或 None（表示验证通过）"""
    if not name:
        return "集合名不能为空！"
    if len(name) > config.max_collection_name_length:
        return f"集合名过长（最多{config.max_collection_name_length}字符）"
    if not is_valid_nickname(name):
        return "集合名只能包含汉字、字母和数字！"
    return None


def extract_at_qq_from_message(msg: Message) -> str | None:
    """从消息中提取第一个 @目标的 QQ 号"""
    return next((seg.data.get("qq") for seg in msg if seg.type == "at"), None)


def extract_at_qq_and_nickname(msg: Message) -> tuple[str | None, str | None]:
    """从消息中提取 @目标的 QQ 号和昵称"""
    at_qq = extract_at_qq_from_message(msg)

    if not at_qq:
        return None, None

    text = msg.extract_plain_text().strip()
    _, _, nickname_part = text.partition("昵称")
    if not nickname_part:
        return at_qq, None

    nickname = nickname_part.strip()
    return at_qq, nickname


def extract_all_at_qq(msg: Message) -> list[str]:
    """从消息中提取所有 @目标的 QQ 号"""
    result = [str(seg.data.get("qq")) for seg in msg if seg.type == "at" and seg.data.get("qq")]
    return list(dict.fromkeys(result))


def parse_delete_command(text: str) -> list[str] | None:
    """解析删除昵称命令，返回要删除的昵称列表"""
    command_match = re.match(r"^(删除昵称|移除昵称)\s+(.+)$", text)
    if not command_match:
        return None

    nickname_part = command_match.group(2).strip()
    nickname_part = re.sub(r"@\d+", "", nickname_part).strip()

    if not nickname_part:
        return None

    return [n.strip() for n in nickname_part.split() if n.strip()]


def parse_collection_name_from_command(text: str, prefix: str) -> str | None:
    """从命令中提取集合名"""
    if not text.startswith(prefix):
        return None
    rest = text[len(prefix) :].strip()
    parts = rest.split()
    return parts[0] if parts else None


def build_delete_reply(success: list[str], not_found: list[str]) -> str:
    """构建删除昵称的回复消息"""
    reply = []
    if success:
        reply.append(f"成功删除昵称：{' '.join(success)}")
    if not_found:
        reply.append(f"以下昵称不存在：{' '.join(not_found)}")

    return "\n".join(reply) if reply else "未删除任何昵称"


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
