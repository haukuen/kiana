from io import BytesIO
from nonebot import on_message, logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from httpx import AsyncClient

from . import bilibili
from . import douyin
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="doubili",
    description="视频解析",
    usage="发送B站抖音视频链接即可下载视频",
    config=Config,
)

async def is_bilibili_link(event: MessageEvent) -> bool:
    message = event.get_plaintext().strip()

    # 检查各种可能的视频格式
    for pattern in bilibili.PATTERNS.values():
        if pattern.search(message):
            return True
    return False

async def is_douyin_link(event: MessageEvent) -> bool:
    """检查是否为抖音视频链接"""
    message = event.get_plaintext().strip()
    return "douyin.com" in message or "iesdouyin.com" in message

bilibili_matcher = on_message(
    rule=Rule(is_bilibili_link),
    priority=5,
)

douyin_matcher = on_message(
    rule=Rule(is_douyin_link),
    priority=5,
)

@bilibili_matcher.handle()
async def handle_bilibili_message(bot: Bot, event: MessageEvent):
    message = event.get_plaintext().strip()

    id_type, video_id = await bilibili.extract_video_id(message)
    if not video_id:
        return

    try:
        # 根据id类型选择参数
        if id_type == "BV":
            video_data = await bilibili.get_video_stream(bvid=video_id)
        else:  # aid
            video_data = await bilibili.get_video_stream(aid=int(video_id))

        if isinstance(video_data, str):
            await bot.send(event, video_data)
        else:
            async with AsyncClient() as client:
                video_response = await client.get(video_data["url"], headers=video_data["headers"])
                video_response.raise_for_status()
                video_bytes = BytesIO(video_response.content)
            await bot.send(event, MessageSegment.video(video_bytes))
    except Exception as e:
        logger.error(f"获取视频失败: {e}")
        await bot.send(event, "获取视频失败，请稍后再试！")

@douyin_matcher.handle()
async def handle_douyin_message(bot: Bot, event: MessageEvent):
    message = event.get_plaintext().strip()
    
    try:
        parser = douyin.DouyinParser()
        video_id = await parser.extract_video_id(message)
        if not video_id:
            return
            
        video_url, title, cover_url = await parser.get_video_url(video_id)
        video_bytes = await parser.download_video(video_url)
        
        await bot.send(event, f"标题: {title}")
        await bot.send(event, MessageSegment.video(video_bytes))
    except Exception as e:
        logger.error(f"处理抖音视频失败: {e}")
        await bot.send(event, "获取视频失败，请稍后再试！")
