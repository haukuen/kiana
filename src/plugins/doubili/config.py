from pydantic import BaseModel


class Config(BaseModel):
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
    MAX_VIDEO_DURATION: int = 120  # 最大视频时长(秒)
