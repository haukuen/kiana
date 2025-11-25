from typing import Literal

from pydantic import BaseModel


class Config(BaseModel):
    # 平台解析开关
    enable_bilibili: bool = True
    enable_douyin: bool = True
    enable_xiaohongshu: bool = True
    xiaohongshu_cookie: str = ""

    # Bilibili 分群配置
    bilibili_group_mode: Literal["all", "whitelist", "blacklist"] = "all"
    bilibili_group_whitelist: list[str] = []
    bilibili_group_blacklist: list[str] = []

    # Douyin 分群配置
    douyin_group_mode: Literal["all", "whitelist", "blacklist"] = "all"
    douyin_group_whitelist: list[str] = []
    douyin_group_blacklist: list[str] = []

    # Xiaohongshu 分群配置
    xiaohongshu_group_mode: Literal["all", "whitelist", "blacklist"] = "all"
    xiaohongshu_group_whitelist: list[str] = []
    xiaohongshu_group_blacklist: list[str] = []

    BILIBILI_API_URL: str = "https://api.bilibili.com/x/player/playurl"
    BILIBILI_VIEW_API_URL: str = "https://api.bilibili.com/x/web-interface/view"

    USER_AGENT: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    REFERER: str = "https://www.bilibili.com/"

    API_HEADERS: dict = {
        "User-Agent": USER_AGENT,
        "Referer": REFERER,
    }

    # 视频相关参数
    VIDEO_QUALITY: int = 64  # 视频清晰度参数

    # 视频限制
    MAX_VIDEO_SIZE: int = 50 * 1024 * 1024  # 最大视频大小(bytes)
    MAX_VIDEO_DURATION: int = 300  # 最大视频时长(秒)
