from nonebot.matcher import Matcher

from .bilibili import bilibili
from .douyin import douyin
from .tiktok import tiktok

resolvers: dict[str, type[Matcher]] = {
    "bilibili": bilibili,
    "douyin": douyin,
    "tiktok": tiktok,
}
