import json
import re
from io import BytesIO
from urllib.parse import unquote

from httpx import AsyncClient
from nonebot import get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from . import bilibili, douyin, xiaohongshu
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="doubili",
    description="视频解析",
    usage="发送B站、抖音、小红书链接即可下载视频或图片",
    config=Config,
)

config = get_plugin_config(Config)


async def is_bilibili_link(event: MessageEvent) -> bool:
    """检查是否为B站链接且B站解析已启用"""
    if not config.enable_bilibili:
        return False

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
    """检查是否为抖音链接且抖音解析已启用"""
    if not config.enable_douyin:
        return False

    message = str(event.message).strip()
    return any(pattern.search(message) for pattern in douyin.PATTERNS.values())


async def is_xiaohongshu_link(event: MessageEvent) -> bool:
    """检查是否为小红书链接且小红书解析已启用"""
    if not config.enable_xiaohongshu:
        return False

    message = str(event.message).strip()

    # 检查卡片消息
    if "CQ:json" in message and config.xiaohongshu_cookie:
        return False  # 暂时解析不了卡片
        try:
            json_str = re.search(r"\[CQ:json,data=(.*?)\]", message)
            if json_str:
                json_data = json.loads(unquote(json_str.group(1).replace("&#44;", ",")))
                if "meta" in json_data and "news" in json_data["meta"]:
                    news = json_data["meta"]["news"]
                    jump_url = news.get("jumpUrl", "")
                    if "xiaohongshu.com" in jump_url or "xhslink.com" in jump_url:
                        return True

        except Exception as e:
            logger.debug(f"解析小红书卡片数据失败: {e}")

    # 检查普通链接
    return any(pattern.search(message) for pattern in xiaohongshu.PATTERNS.values())


douyin_matcher = on_message(
    rule=Rule(is_douyin_link),
    priority=5,
)


@douyin_matcher.handle()
async def handle_douyin_message(bot: Bot, event: MessageEvent):
    """处理抖音消息"""
    message = str(event.message).strip()
    video_id = await douyin.extract_video_id(message)

    if not video_id:
        await douyin_matcher.finish("未找到有效的抖音视频ID")
        return

    try:
        video_info = await douyin.get_video_info(video_id)
        if isinstance(video_info, str):
            await douyin_matcher.finish(video_info)
            return

        # 发送视频信息
        await douyin_matcher.send(f"标题: {video_info['title']}")

        # 下载并发送视频
        async with AsyncClient() as client:
            response = await client.get(video_info["url"], headers=video_info["headers"])
            response.raise_for_status()

            video_data = BytesIO(response.content)
            video_segment = MessageSegment.video(video_data)
            await douyin_matcher.finish(video_segment)

    except Exception as e:
        logger.error(f"处理抖音视频失败: {e}")
        await douyin_matcher.finish(f"处理视频失败: {e}")


# 小红书消息匹配器
xiaohongshu_matcher = on_message(
    rule=Rule(is_xiaohongshu_link),
    priority=5,
)


@xiaohongshu_matcher.handle()
async def handle_xiaohongshu_message(bot: Bot, event: MessageEvent):
    """处理小红书消息"""
    message = str(event.message).strip()
    url = ""

    # 先尝试从卡片消息中提取URL（需要配置cookie）
    if "CQ:json" in message and config.xiaohongshu_cookie:
        try:
            json_str = re.search(r"\[CQ:json,data=(.*?)\]", message)
            if json_str:
                json_data = json.loads(unquote(json_str.group(1).replace("&#44;", ",")))
                if "meta" in json_data and "news" in json_data["meta"]:
                    news = json_data["meta"]["news"]
                    jump_url = news.get("jumpUrl", "")
                    if "xiaohongshu.com" in jump_url or "xhslink.com" in jump_url:
                        # 清理URL，移除多余的参数
                        url = await xiaohongshu.extract_url(jump_url)
        except Exception as e:
            logger.debug(f"从卡片消息提取小红书链接失败: {e}")
    elif "CQ:json" in message and not config.xiaohongshu_cookie:
        logger.debug("检测到小红书卡片消息，但未配置cookie，跳过卡片解析")

    # 如果卡片消息中没有找到，再从普通文本中提取
    if not url:
        url = await xiaohongshu.extract_url(message)

    if not url:
        await xiaohongshu_matcher.finish("未找到有效的小红书链接")
        return

    try:
        note_info = await xiaohongshu.get_note_info(url)
        if isinstance(note_info, str):
            await xiaohongshu_matcher.finish(note_info)
            return

        # 发送笔记信息
        info_text = f"标题: {note_info['title']}\n作者: {note_info['author']}"
        await xiaohongshu_matcher.send(info_text)

        # 处理图片
        if note_info["pic_urls"]:
            for pic_url in note_info["pic_urls"][:9]:  # 最多发送9张图片
                try:
                    async with AsyncClient() as client:
                        response = await client.get(pic_url, timeout=30.0)
                        response.raise_for_status()

                        image_data = BytesIO(response.content)
                        image_segment = MessageSegment.image(image_data)
                        await xiaohongshu_matcher.send(image_segment)
                except Exception as e:
                    logger.warning(f"下载图片失败: {e}")
                    continue

        # 处理视频
        elif note_info["video_url"]:
            async with AsyncClient() as client:
                response = await client.get(note_info["video_url"], timeout=60.0)
                video_data = BytesIO(response.content)
                video_segment = MessageSegment.video(video_data)
                await xiaohongshu_matcher.send(video_segment)
        else:
            await xiaohongshu_matcher.finish("该笔记没有可下载的媒体内容")

    except Exception as e:
        logger.error(f"处理小红书笔记失败: {e}")
        await xiaohongshu_matcher.finish(f"处理笔记失败: {e}")
