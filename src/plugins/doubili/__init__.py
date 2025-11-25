import json
import re
from io import BytesIO
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from httpx import AsyncClient
from nonebot import get_driver, get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, MessageSegment
from nonebot.exception import MatcherException
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from ..group_permission import create_platform_rule
from . import bilibili, douyin, xiaohongshu
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="doubili",
    description="视频解析",
    usage="发送B站、抖音、小红书链接即可下载视频或图片",
    config=Config,
)

config = get_plugin_config(Config)

# JSON CQ码提取正则（限制长度防止 ReDoS）
_JSON_CARD_PATTERN = re.compile(r"\[CQ:json,data=([^\]]{1,10000})\]")


def parse_json_card_from_segment(event: MessageEvent) -> dict | None:
    """从 MessageSegment 解析 JSON 卡片（官方推荐方式）

    直接从消息段对象中提取 JSON 数据，无需处理 CQ 码转义。

    Args:
        event: 消息事件

    Returns:
        解析后的 JSON 对象，解析失败返回 None
    """
    for seg in event.message:
        if seg.type == "json":
            data = seg.data.get("data")
            if data:
                try:
                    result = json.loads(data) if isinstance(data, str) else data
                    logger.debug("JSON卡片解析 - 使用 MessageSegment 方式成功")
                    return result
                except json.JSONDecodeError as e:
                    logger.debug(f"JSON卡片解析 - MessageSegment 方式 JSON 解码失败: {e}")
    return None


def parse_json_card_from_cqcode(message: str) -> dict | None:
    """从 CQ 码解析 JSON 卡片（回退方式）

    处理常见的转义字符（如 &#44;）和 URL 编码。

    Args:
        message: 包含 [CQ:json,data=...] 的消息文本

    Returns:
        解析后的 JSON 对象，解析失败返回 None
    """
    if "CQ:json" not in message:
        return None

    try:
        match = _JSON_CARD_PATTERN.search(message)
        if not match:
            return None

        json_str = match.group(1)
        # 处理转义字符：&#44; → ,
        json_str = json_str.replace("&#44;", ",")
        # 处理 URL 编码
        json_str = unquote(json_str)

        result = json.loads(json_str)
        logger.debug("JSON卡片解析 - 使用 CQ码正则 回退方式成功")
        return result

    except (json.JSONDecodeError, ValueError) as e:
        logger.debug(f"JSON卡片解析 - CQ码正则 方式失败: {e}")
        return None


def parse_json_card(event_or_message: MessageEvent | str) -> dict | None:
    """解析 JSON 卡片消息（优先官方方式，失败回退 CQ 码）

    Args:
        event_or_message: MessageEvent 对象或消息字符串

    Returns:
        解析后的 JSON 对象，解析失败返回 None
    """
    # 优先使用 MessageSegment 官方方式
    if isinstance(event_or_message, MessageEvent):
        result = parse_json_card_from_segment(event_or_message)
        if result is not None:
            return result
        # 回退到 CQ 码方式
        message = str(event_or_message.message)
    else:
        message = event_or_message

    # CQ 码方式（回退或直接传入字符串的情况）
    result = parse_json_card_from_cqcode(message)
    if result is not None:
        return result

    logger.debug("JSON卡片解析 - 两种方式均失败")
    return None


async def get_redirect_url(url: str, timeout: float = 10.0) -> str:
    """获取重定向后的URL

    Args:
        url: 原始 URL
        timeout: 超时时间（秒）

    Returns:
        重定向后的 URL 字符串
    """
    async with AsyncClient(follow_redirects=True, timeout=timeout) as client:
        response = await client.get(url)
        return str(response.url)


async def download_media(url: str, headers: dict | None = None) -> BytesIO:
    """下载媒体文件到内存

    Args:
        url: 媒体文件 URL
        headers: 可选的请求头

    Returns:
        包含媒体数据的 BytesIO 对象
    """
    async with AsyncClient(follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return BytesIO(response.content)


# 创建各平台的规则检查函数
_bilibili_group_rule = create_platform_rule(lambda: config, "bilibili")
_douyin_group_rule = create_platform_rule(lambda: config, "douyin")
_xiaohongshu_group_rule = create_platform_rule(lambda: config, "xiaohongshu")


driver = get_driver()


def _log_video_processing(
    platform: str, event: MessageEvent, video_id: str, id_type: str = "", url_type: str = "视频ID"
) -> None:
    """统一视频处理监控日志

    Args:
        platform: 平台名称 (Bilibili/Douyin/Xiaohongshu)
        event: 消息事件
        video_id: 视频ID或URL
        id_type: ID类型 (bvid/avid/等)，为空则不显示
        url_type: ID类型描述 (视频ID/URL)
    """
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else "私聊"
    id_info = f" ({id_type})" if id_type else ""
    logger.info(
        f"处理{platform}链接 | 用户: {event.user_id} | 群组: {group_id} | "
        f"{url_type}: {video_id}{id_info}"
    )


async def is_bilibili_link(event: MessageEvent) -> bool:
    """检查是否为B站链接（仅内容检查）"""
    # 根据消息类型选择检查方式
    has_json = any(seg.type == "json" for seg in event.message)
    has_text = any(seg.type == "text" and seg.data.get("text", "").strip() for seg in event.message)

    # 检查 JSON 卡片
    if has_json:
        json_data = parse_json_card(event)
        if json_data and "meta" in json_data and "detail_1" in json_data["meta"]:
            detail = json_data["meta"]["detail_1"]
            # 验证 appid 是 B站的
            if detail.get("appid") == "1109937557":
                return True

    # 检查普通文本链接
    if has_text:
        message = event.get_plaintext().strip()
        return any(pattern.search(message) for pattern in bilibili.PATTERNS.values())

    return False


bilibili_matcher = on_message(
    rule=Rule(_bilibili_group_rule, is_bilibili_link),
    priority=5,
    block=True,  # 匹配成功后阻止后续 matcher 执行,避免重复处理
)


@bilibili_matcher.handle()
async def handle_bilibili_message(
    bot: Bot,
    event: MessageEvent,
):
    """处理Bilibili消息"""
    message = str(event.message).strip()

    # 记录原始消息用于调试（无长度限制）
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else "私聊"
    logger.debug(
        f"Bilibili匹配触发 | 用户: {event.user_id} | 群组: {group_id} | 原始消息: {message}"
    )

    id_type, video_id = await bilibili.extract_video_id(message)
    if not video_id:
        logger.debug("未提取到有效的视频ID，跳过处理")
        return

    _log_video_processing("Bilibili", event, video_id, id_type)

    try:
        # 1. 获取视频流信息
        if id_type == "bvid":
            video_data = await bilibili.get_video_stream(bvid=video_id)
        else:  # avid
            video_data = await bilibili.get_video_stream(avid=int(video_id))

        if isinstance(video_data, str):
            # 记录详细错误到日志
            logger.warning(f"Bilibili视频获取失败: {video_data}")
            # 向用户发送友好提示
            await bilibili_matcher.finish("视频获取失败")

        # 2. 下载并发送视频
        video_bytes = await download_media(video_data["url"], headers=video_data["headers"])
        await bilibili_matcher.send(MessageSegment.video(video_bytes))

    except MatcherException:
        raise
    except Exception as e:
        # 记录详细错误到日志（包含堆栈）
        logger.error(f"处理Bilibili视频失败: {e}", exc_info=True)
        # 向用户发送友好提示
        await bilibili_matcher.finish("视频处理失败，请稍后重试")


async def is_douyin_link(event: MessageEvent) -> bool:
    """检查是否为抖音链接（仅内容检查）"""
    message = str(event.message).strip()
    return any(pattern.search(message) for pattern in douyin.PATTERNS.values())


async def is_xiaohongshu_link(event: MessageEvent) -> bool:
    """检查是否为小红书链接（仅内容检查）"""
    # 根据消息类型选择检查方式
    has_json = any(seg.type == "json" for seg in event.message)
    has_text = any(seg.type == "text" and seg.data.get("text", "").strip() for seg in event.message)

    # 检查 JSON 卡片（需要配置 cookie）
    if has_json and config.xiaohongshu_cookie:
        json_data = parse_json_card(event)
        if json_data and "meta" in json_data and "news" in json_data["meta"]:
            news = json_data["meta"]["news"]
            jump_url = news.get("jumpUrl", "")
            # 简单验证：检查是否包含小红书域名
            if jump_url and ("xiaohongshu.com" in jump_url or "xhslink.com" in jump_url):
                return True

    # 检查普通文本链接
    if has_text:
        message = event.get_plaintext().strip()
        return any(pattern.search(message) for pattern in xiaohongshu.PATTERNS.values())

    return False


douyin_matcher = on_message(
    rule=Rule(_douyin_group_rule, is_douyin_link),
    priority=5,
    block=True,  # 匹配成功后阻止后续 matcher 执行,避免重复处理
)


@douyin_matcher.handle()
async def handle_douyin_message(
    bot: Bot,
    event: MessageEvent,
):
    """处理抖音消息"""
    message = str(event.message).strip()
    video_id = await douyin.extract_video_id(message)

    if not video_id:
        await douyin_matcher.finish("未找到有效的视频链接")

    _log_video_processing("Douyin", event, video_id)

    try:
        # 1. 获取视频信息
        video_info = await douyin.get_video_info(video_id)
        if isinstance(video_info, str):
            # 记录详细错误到日志
            logger.warning(f"抖音视频获取失败: {video_info}")
            # 向用户发送友好提示
            await douyin_matcher.finish("视频获取失败")

        # 2. 发送标题
        await douyin_matcher.send(f"{video_info['title']}")

        # 3. 下载视频
        video_data = await download_media(video_info["url"], headers=video_info["headers"])

        # 4. 发送视频（超时处理）
        try:
            await douyin_matcher.finish(MessageSegment.video(video_data))
        except MatcherException:
            raise
        except Exception as send_error:
            error_str = str(send_error)
            if "timeout" in error_str.lower() or "NetWorkError" in error_str:
                # 超时可能已发送成功，只记录日志
                logger.warning(f"发送视频超时，但可能已发送: {send_error}")
            else:
                # 其他发送错误记录详细日志
                logger.error(f"发送视频失败: {send_error}", exc_info=True)
                # 友好提示用户
                await douyin_matcher.finish("视频发送失败")
            return

    except MatcherException:
        raise
    except Exception as e:
        # 记录详细错误到日志（包含堆栈）
        logger.error(f"处理抖音视频失败: {e}", exc_info=True)
        # 向用户发送友好提示
        await douyin_matcher.finish("视频处理失败，请稍后重试")


# 小红书消息匹配器
xiaohongshu_matcher = on_message(
    rule=Rule(_xiaohongshu_group_rule, is_xiaohongshu_link),
    priority=5,
    block=True,  # 匹配成功后阻止后续 matcher 执行,避免重复处理
)


async def extract_url_from_card_message(event_or_message: MessageEvent | str) -> str:
    """从卡片消息中提取小红书URL

    注意: 调用此函数前应确保已通过 is_xiaohongshu_link 检查 cookie 配置
    """
    # 使用工具函数解析 JSON 卡片（支持 event 和字符串）
    json_data = parse_json_card(event_or_message)
    if not json_data or "meta" not in json_data or "news" not in json_data["meta"]:
        return ""

    news = json_data["meta"]["news"]
    jump_url = news.get("jumpUrl", "")

    if "xiaohongshu.com" not in jump_url and "xhslink.com" not in jump_url:
        return ""

    return await process_xiaohongshu_url(jump_url)


async def process_xiaohongshu_url(jump_url: str) -> str:
    """处理小红书URL，包括短链接解析和参数提取"""
    # 处理短链接
    if "xhslink" in jump_url:
        # 基础安全检查
        try:
            parsed = urlparse(jump_url)
            if parsed.scheme not in {"http", "https"}:
                logger.warning(f"小红书短链接协议异常: {parsed.scheme}")
                return ""
            if len(jump_url) > 2048:
                logger.warning(f"小红书短链接过长: {len(jump_url)} 字符")
                return ""
        except Exception as e:
            logger.warning(f"小红书短链接解析异常: {jump_url} - {e}")
            return ""

        # 使用工具函数解析短链接
        try:
            jump_url = await get_redirect_url(jump_url, timeout=10.0)
        except Exception as e:
            logger.warning(f"小红书短链接重定向失败: {jump_url} - {e}")
            return ""

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
        final_url = f"https://www.xiaohongshu.com/explore/{xhs_id}?xsec_source={xsec_source}&xsec_token={xsec_token}"
    else:
        final_url = f"https://www.xiaohongshu.com/explore/{xhs_id}?xsec_source={xsec_source}"

    return final_url


async def download_image_concurrent(
    pic_url: str,
    max_concurrent: int = 5,
) -> MessageSegment | None:
    """并发下载单张图片

    Args:
        pic_url: 图片URL
        max_concurrent: 最大并发数（通过semaphore控制）

    Returns:
        MessageSegment: 下载成功返回图片消息段
        None: 下载失败返回None
    """
    try:
        async with AsyncClient(follow_redirects=True) as client:
            response = await client.get(pic_url)
            response.raise_for_status()

            image_data = BytesIO(response.content)
            return MessageSegment.image(image_data)
    except Exception as e:
        logger.warning(f"下载图片失败 {pic_url}: {e}")
        return None


async def download_images_concurrent(
    pic_urls: list[str],
    max_concurrent: int = 5,
) -> list[MessageSegment]:
    """并发下载多张图片

    使用asyncio.gather实现并发下载，显著提升多图下载性能。
    9张图片下载时间从~18秒降低到~3秒（取决于网络状况）。

    Args:
        pic_urls: 图片URL列表
        max_concurrent: 最大并发数，防止过多并发导致网络拥塞

    Returns:
        成功下载的图片消息段列表（失败的已过滤）
    """
    # 创建semaphore限制并发数
    semaphore = asyncio.Semaphore(max_concurrent)

    async def download_with_semaphore(url: str) -> MessageSegment | None:
        async with semaphore:
            return await download_image_concurrent(url)

    # 创建所有下载任务
    tasks = [download_with_semaphore(url) for url in pic_urls]

    # 并发执行所有任务，return_exceptions=True防止单个失败影响全局
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 过滤成功的结果（排除None和Exception）
    image_segments = []
    for result in results:
        if isinstance(result, MessageSegment):
            image_segments.append(result)
        elif isinstance(result, Exception):
            logger.warning(f"图片下载异常: {result}")
        # None值直接忽略

    logger.info(f"成功下载 {len(image_segments)}/{len(pic_urls)} 张图片")
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


def create_forward_nodes(
    bot: Bot, info_text: str, media_segments: list[MessageSegment] | None = None
) -> list[dict[str, Any]]:
    """创建合并转发消息节点"""
    forward_nodes: list[dict[str, Any]] = []

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
async def handle_xiaohongshu_message(
    bot: Bot,
    event: MessageEvent,
):
    """处理小红书消息"""
    message = str(event.message).strip()

    # 先尝试从卡片消息中提取URL（传入 event 以使用官方方式）
    url = await extract_url_from_card_message(event)

    if not url:
        url = await xiaohongshu.extract_url(message)

    if not url:
        await xiaohongshu_matcher.finish("未找到有效的笔记链接")

    _log_video_processing("Xiaohongshu", event, url[:50] + "...", url_type="URL")

    try:
        # 1. 获取笔记信息
        note_info = await xiaohongshu.get_note_info(url)
        if isinstance(note_info, str):
            # 记录详细错误到日志
            logger.warning(f"小红书笔记获取失败: {note_info}")
            # 向用户发送友好提示
            await xiaohongshu_matcher.finish("笔记获取失败")

        info_text = f"{note_info['title']}\n作者: {note_info['author']}"

        # 2. 根据内容类型处理
        if note_info["pic_urls"]:
            # 处理图片内容 - 使用并发下载提升性能
            pic_urls = note_info["pic_urls"][:9]  # 最多处理9张图片
            logger.info(f"图片数量{len(pic_urls)}张，使用并发下载（max_concurrent=5）")

            image_segments = await download_images_concurrent(pic_urls, max_concurrent=5)
            forward_nodes = create_forward_nodes(bot, info_text, image_segments)
            await send_forward_message(bot, event, forward_nodes)

        elif note_info["video_url"]:
            # 处理视频内容
            video_data = await download_media(note_info["video_url"])
            video_segment = MessageSegment.video(video_data)

            forward_nodes = create_forward_nodes(bot, info_text, [video_segment])
            await send_forward_message(bot, event, forward_nodes)

        else:
            # 处理纯文字内容
            forward_nodes = create_forward_nodes(bot, info_text)
            await send_forward_message(bot, event, forward_nodes)

    except MatcherException:
        raise
    except Exception as e:
        # 记录详细错误到日志（包含堆栈）
        logger.error(f"处理小红书笔记失败: {e}", exc_info=True)
        # 向用户发送友好提示
        await xiaohongshu_matcher.finish("内容处理失败，请稍后重试")
