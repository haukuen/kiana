import json
import re
from io import BytesIO
from urllib.parse import unquote

import httpx
from httpx import AsyncClient
from nonebot import get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, MessageSegment
from nonebot.exception import MatcherException
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
    except MatcherException:
        raise
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
        await douyin_matcher.send(f"{video_info['title']}")

        # 下载并发送视频
        async with AsyncClient() as client:
            response = await client.get(video_info["url"], headers=video_info["headers"])
            response.raise_for_status()

            video_data = BytesIO(response.content)
            video_segment = MessageSegment.video(video_data)
            await douyin_matcher.finish(video_segment)

    except MatcherException:
        raise
    except Exception as e:
        logger.error(f"处理抖音视频失败: {e}")
        await douyin_matcher.finish(f"处理视频失败: {e}")


# 小红书消息匹配器
xiaohongshu_matcher = on_message(
    rule=Rule(is_xiaohongshu_link),
    priority=5,
)


async def extract_url_from_card_message(message: str) -> str:
    """从卡片消息中提取小红书URL"""
    if "CQ:json" not in message:
        return ""

    if not config.xiaohongshu_cookie or not config.xiaohongshu_cookie.strip():
        logger.debug("检测到小红书卡片消息，但未配置有效cookie，跳过卡片解析")
        return ""

    try:
        json_str = re.search(r"\[CQ:json,data=(.*?)\]", message)
        if not json_str:
            return ""

        json_data = json.loads(unquote(json_str.group(1).replace("&#44;", ",")))
        if "meta" not in json_data or "news" not in json_data["meta"]:
            return ""

        news = json_data["meta"]["news"]
        jump_url = news.get("jumpUrl", "")

        if "xiaohongshu.com" not in jump_url and "xhslink.com" not in jump_url:
            return ""

        return await process_xiaohongshu_url(jump_url)

    except Exception as e:
        logger.debug(f"从卡片消息提取小红书链接失败: {e}")
        return ""


async def process_xiaohongshu_url(jump_url: str) -> str:
    """处理小红书URL，包括短链接解析和参数提取"""
    import html
    from urllib.parse import parse_qs, urlparse

    # 处理短链接
    if "xhslink" in jump_url:
        async with httpx.AsyncClient() as client:
            response = await client.get(jump_url, follow_redirects=True)
            jump_url = str(response.url)

    # 提取笔记ID
    pattern = r"(?:/explore/|/discovery/item/|source=note&noteId=)(\w+)"
    matched = re.search(pattern, jump_url)

    if not matched:
        # 如果无法提取ID，回退到原来的方法
        return await xiaohongshu.extract_url(jump_url)

    xhs_id = matched.group(1)
    # 解析URL参数
    parsed_url = urlparse(jump_url)
    # 解码HTML实体
    decoded_query = html.unescape(parsed_url.query)
    params = parse_qs(decoded_query)

    # 提取xsec_source和xsec_token
    xsec_source = params.get("xsec_source", [None])[0] or "pc_feed"
    xsec_token = params.get("xsec_token", [None])[0]

    # 构造完整URL
    if xsec_token:
        return f"https://www.xiaohongshu.com/explore/{xhs_id}?xsec_source={xsec_source}&xsec_token={xsec_token}"

    return f"https://www.xiaohongshu.com/explore/{xhs_id}?xsec_source={xsec_source}"


async def download_images(pic_urls: list) -> list:
    """下载图片并返回图片段列表"""
    image_segments = []
    for pic_url in pic_urls:
        try:
            async with AsyncClient() as client:
                response = await client.get(pic_url, timeout=30.0)
                response.raise_for_status()

                image_data = BytesIO(response.content)
                image_segment = MessageSegment.image(image_data)
                image_segments.append(image_segment)
        except Exception as e:
            logger.warning(f"下载图片失败: {e}")
            continue
    return image_segments


async def send_forward_message(bot: Bot, event: MessageEvent, forward_nodes: list):
    """发送合并转发消息"""
    if isinstance(event, GroupMessageEvent):
        await bot.call_api(
            "send_group_forward_msg",
            group_id=event.group_id,
            messages=forward_nodes,
        )
    else:
        await bot.call_api(
            "send_private_forward_msg",
            user_id=event.user_id,
            messages=forward_nodes,
        )


async def create_forward_nodes(
    bot: Bot, info_text: str, media_segments: list[MessageSegment] | None = None
) -> list[dict]:
    """创建合并转发消息节点"""
    forward_nodes = []

    # 添加文字内容节点
    text_node = {
        "type": "node",
        "data": {"name": "", "uin": bot.self_id, "content": info_text},
    }
    forward_nodes.append(text_node)

    # 添加媒体内容节点
    if media_segments:
        for media_seg in media_segments:
            node = {
                "type": "node",
                "data": {"name": "", "uin": bot.self_id, "content": media_seg},
            }
            forward_nodes.append(node)

    return forward_nodes


@xiaohongshu_matcher.handle()
async def handle_xiaohongshu_message(bot: Bot, event: MessageEvent):
    """处理小红书消息"""
    message = str(event.message).strip()

    # 先尝试从卡片消息中提取URL
    url = await extract_url_from_card_message(message)

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

        info_text = f"{note_info['title']}\n作者: {note_info['author']}"

        if note_info["pic_urls"]:
            # 处理图片内容
            pic_urls = note_info["pic_urls"][:9]  # 最多处理9张图片
            logger.info(f"图片数量{len(pic_urls)}张，合并转发所有图片")

            image_segments = await download_images(pic_urls)
            forward_nodes = await create_forward_nodes(bot, info_text, image_segments)
            await send_forward_message(bot, event, forward_nodes)

        elif note_info["video_url"]:
            # 处理视频内容
            async with AsyncClient() as client:
                response = await client.get(note_info["video_url"], timeout=60.0)
                video_data = BytesIO(response.content)
                video_segment = MessageSegment.video(video_data)

            forward_nodes = await create_forward_nodes(bot, info_text, [video_segment])
            await send_forward_message(bot, event, forward_nodes)

        else:
            # 处理纯文字内容
            forward_nodes = await create_forward_nodes(bot, info_text)
            await send_forward_message(bot, event, forward_nodes)

    except MatcherException:
        raise
    except Exception as e:
        logger.error(f"处理小红书笔记失败: {e}")
        await xiaohongshu_matcher.finish(f"处理笔记失败: {e}")
