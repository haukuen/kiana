import json
import re
from io import BytesIO
from urllib.parse import unquote

from httpx import AsyncClient
from nonebot import logger, on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from . import bilibili, douyin
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="doubili",
    description="视频解析",
    usage="发送B站抖音视频链接即可下载视频",
    config=Config,
)


async def is_bilibili_link(event: MessageEvent) -> bool:
    message = str(event.message).strip()

    if "CQ:json" in message:
        try:
            json_str = re.search(r"\[CQ:json,data=(.*?)\]", message)
            if json_str:
                # 处理转义字符
                json_data = json.loads(unquote(json_str.group(1).replace("&#44;", ",")))
                if "meta" in json_data and "detail_1" in json_data["meta"]:
                    detail = json_data["meta"]["detail_1"]
                    # 验证appid是B站的
                    if detail.get("appid") == "1109937557":
                        return True

        except Exception as e:
            logger.debug(f"解析小程序数据失败: {e}")

    # 检查普通链接
    return any(pattern.search(message) for pattern in bilibili.PATTERNS.values())


bilibili_matcher = on_message(
    rule=Rule(is_bilibili_link),
    priority=5,
)


@bilibili_matcher.handle()
async def handle_bilibili_message(bot: Bot, event: MessageEvent):
    message = str(event.message).strip()

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


async def is_douyin_link(event: MessageEvent) -> bool:
    message = event.get_plaintext().strip()
    return bool(douyin.PATTERNS["douyin"].search(message))


douyin_matcher = on_message(
    rule=Rule(is_douyin_link),
    priority=5,
)


@douyin_matcher.handle()
async def handle_douyin_message(bot: Bot, event: MessageEvent):
    message = event.get_plaintext().strip()

    video_id = await douyin.extract_video_id(message)
    if not video_id:
        return

    try:
        video_data = await douyin.get_video_info(video_id)

        if isinstance(video_data, str):
            await bot.send(event, video_data)
        else:
            async with AsyncClient() as client:
                video_response = await client.get(video_data["url"], headers=video_data["headers"])
                video_response.raise_for_status()
                video_bytes = BytesIO(video_response.content)
            await bot.send(event, video_data["title"])
            await bot.send(event, MessageSegment.video(video_bytes))
    except Exception as e:
        logger.error(f"获取视频失败: {e}")
        await bot.send(event, "获取视频失败，请稍后再试！")
