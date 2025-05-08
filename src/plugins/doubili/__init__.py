import re
from io import BytesIO
from httpx import AsyncClient

from nonebot import get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="doubili",
    description="",
    usage="",
    config=Config,
)

config = get_plugin_config(Config)

BILIBILI_URL_PATTERN = re.compile(r"https?://www\.bilibili\.com/video/(?P<bvid>BV[\w]+)")


async def is_bilibili_link(event: MessageEvent) -> bool:
    message = event.get_plaintext().strip()
    return bool(BILIBILI_URL_PATTERN.search(message))


bilibili = on_message(
    rule=Rule(is_bilibili_link),
    priority=5,
)


@bilibili.handle()
async def handle_bilibili_message(bot: Bot, event: MessageEvent):
    message = event.get_plaintext().strip()
    match = BILIBILI_URL_PATTERN.search(message)

    if not match:
        return

    bvid = match.group("bvid")
    try:
        video_data = await get_video_stream(bvid)
        if isinstance(video_data, str):
            await bot.send(event, video_data)
        else:
            # 下载视频为字节流
            async with AsyncClient() as client:
                video_response = await client.get(video_data["url"], headers=video_data["headers"])
                video_response.raise_for_status()
                video_bytes = BytesIO(video_response.content)

            await bot.send(event, MessageSegment.video(video_bytes))
    except Exception as e:
        logger.error(f"获取视频失败: {e}")
        await bot.send(event, "获取视频失败，请稍后再试！")


async def get_video_info(bvid: str = None, aid: int = None):
    """获取 Bilibili 视频详细信息"""
    if not bvid and not aid:
        raise ValueError("必须提供 bvid 或 aid 参数！")

    api_url = "https://api.bilibili.com/x/web-interface/view"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    }
    params = {"bvid": bvid, "aid": aid}

    async with AsyncClient() as client:
        response = await client.get(api_url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()

        if data.get("code") != 0:
            raise Exception(f"获取视频信息失败：{data.get('message', '未知错误')}！")

        return data["data"]


async def get_video_stream(bvid: str):
    """获取 Bilibili 视频流信息"""
    video_info = await get_video_info(bvid=bvid)
    cid = video_info.get("cid")
    if not cid:
        raise Exception("未能获取视频的 cid！")

    api_url = config.BILIBILI_API_URL
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    }
    params = {
        "bvid": bvid,
        "cid": cid,
        "qn": 64,  # 清晰度参数
    }

    async with AsyncClient() as client:
        response = await client.get(api_url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()

        if data.get("code") != 0:
            return f"获取视频信息失败：{data.get('message', '未知错误')}"

        video_url = data["data"]["durl"][0]["url"]

        video_headers = {
            "User-Agent": headers["User-Agent"],
            "Referer": headers["Referer"],
        }

        return {"url": video_url, "headers": video_headers}
