from pydantic import BaseModel


class Config(BaseModel):
    BILIBILI_API_URL: str = "https://api.bilibili.com/x/player/playurl"
    BILIBILI_API_HEADERS: dict = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    }
