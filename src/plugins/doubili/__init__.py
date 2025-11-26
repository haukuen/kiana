import asyncio
import html
import json
import re
from io import BytesIO
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from httpx import AsyncClient
from nonebot import get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, MessageSegment
from nonebot.exception import MatcherException
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from nonebot.typing import T_State

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

def parse_card_message(event: MessageEvent) -> tuple[str | None, str | None]:
    """从腾讯卡片消息提取链接和标题（统一解析入口）

    从 MessageSegment 中提取 JSON 卡片数据，根据 app 字段分流解析。

    支持的卡片类型：
    - com.tencent.miniapp_01 (小程序): meta.detail_1.qqdocurl
    - com.tencent.tuwen.lua (图文分享): meta.{view}.jumpUrl
    - com.tencent.music.lua (音乐分享): meta.{view}.jumpUrl
    - com.tencent.structmsg (结构化消息): meta.{view}.jumpUrl
    - com.tencent.channel.share (频道分享): meta.detail.link

    Args:
        event: 消息事件

    Returns:
        (url, title) 元组，未找到返回 (None, None)
    """
    # 从 MessageSegment 提取 JSON 卡片数据
    card_data = None
    for seg in event.message:
        if seg.type == "json":
            if data := seg.data.get("data"):
                try:
                    card_data = json.loads(data) if isinstance(data, str) else data
                    break
                except json.JSONDecodeError as e:
                    logger.debug(f"JSON卡片解析失败: {e}")

    if not card_data:
        return (None, None)

    app = card_data.get("app", "")
    meta = card_data.get("meta", {})
    view = card_data.get("view", "")

    url, title = None, None

    match app:
        case "com.tencent.miniapp_01":
            # 小程序卡片
            detail = meta.get("detail_1", {})
            url = detail.get("qqdocurl") or detail.get("url")
            title = detail.get("desc") or detail.get("title")
            if url:
                logger.debug(f"腾讯卡片解析 - 小程序卡片，app={app}")

        case "com.tencent.tuwen.lua" | "com.tencent.music.lua":
            # 图文/音乐分享
            view_data = meta.get(view, {}) if view else meta.get("news", {})
            url = view_data.get("jumpUrl")
            title = view_data.get("title")
            if url:
                logger.debug(f"腾讯卡片解析 - 图文/音乐分享，app={app}, view={view}")

        case "com.tencent.structmsg":
            # 结构化消息（旧版协议）
            view_data = meta.get(view, {}) if view else meta.get("news", {})
            url = view_data.get("jumpUrl")
            title = view_data.get("title")
            if url:
                logger.debug(f"腾讯卡片解析 - 结构化消息，app={app}, view={view}")

        case "com.tencent.channel.share":
            # 频道分享
            detail = meta.get("detail", {})
            url = detail.get("link")
            title = detail.get("title")
            if url:
                logger.debug(f"腾讯卡片解析 - 频道分享，app={app}")

        case _:
            # 通用回退：尝试 news 路径
            if news := meta.get("news"):
                url = news.get("jumpUrl")
                title = news.get("title")
                if url:
                    logger.debug(f"腾讯卡片解析 - 通用回退(news)，app={app}")

    # 处理反斜杠转义
    if url:
        url = url.replace("\\", "/")

    if not url:
        logger.debug(f"腾讯卡片解析 - 未找到 URL，app={app}, meta keys={list(meta.keys())}")

    return (url, title)


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


def _log_matcher_event(
    platform: str,
    event: MessageEvent,
    content_id: str | None = None,
    id_type: str = "",
    success: bool = True,
    message: str = "",
) -> None:
    """统一 Matcher 事件日志

    Args:
        platform: 平台名称 (Bilibili/Douyin/Xiaohongshu)
        event: 消息事件
        content_id: 内容ID或URL（成功时必填）
        id_type: ID类型 (bvid/avid/等)，为空则不显示
        success: 是否成功提取
        message: 额外信息（失败时的原因等）
    """
    group_id = event.group_id if isinstance(event, GroupMessageEvent) else "私聊"
    base_info = f"{platform} | 用户: {event.user_id} | 群组: {group_id}"

    if success and content_id:
        id_info = f" ({id_type})" if id_type else ""
        logger.info(f"处理{base_info} | ID: {content_id}{id_info}")
    else:
        extra = f" | {message}" if message else ""
        logger.debug(f"跳过{base_info}{extra}")


async def is_bilibili_link(event: MessageEvent, state: T_State) -> bool:
    """检查是否为B站链接（仅内容检查）

    检测阶段提取的 URL 会存入 state["card_url"]，避免处理阶段重复解析。
    """
    # 1. 尝试卡片消息
    if any(seg.type == "json" for seg in event.message):
        url, title = parse_card_message(event)
        if url and ("bilibili.com" in url or "b23.tv" in url):
            state["card_url"] = url
            state["card_title"] = title
            return True

    # 2. 尝试文本消息
    if any(seg.type == "text" and seg.data.get("text", "").strip() for seg in event.message):
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
    state: T_State,
):
    """处理Bilibili消息"""
    # 优先使用检查阶段缓存的卡片URL，回退到原始消息
    text_to_parse = state.get("card_url") or str(event.message).strip()
    id_type, video_id = await bilibili.extract_video_id(text_to_parse)
    if not video_id:
        _log_matcher_event("Bilibili", event, success=False, message="未提取到视频ID")
        return

    _log_matcher_event("Bilibili", event, video_id, id_type)

    try:
        # 1. 获取视频流信息
        if id_type == "bvid":
            video_data = await bilibili.get_video_stream(bvid=video_id)
        else:  # avid
            video_data = await bilibili.get_video_stream(avid=int(video_id))

        if isinstance(video_data, str):
            # 记录详细错误到日志
            logger.warning(f"Bilibili视频获取失败: {video_data}")
            # 向用户发送详细错误信息
            await bilibili_matcher.finish(video_data)

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


async def is_xiaohongshu_link(event: MessageEvent, state: T_State) -> bool:
    """检查是否为小红书链接（仅内容检查）

    检测阶段提取的 URL 会存入 state["card_url"]，避免处理阶段重复解析。
    """
    # 1. 尝试卡片消息（检测阶段不要求 cookie，处理阶段再验证）
    if any(seg.type == "json" for seg in event.message):
        url, title = parse_card_message(event)
        if url and ("xiaohongshu.com" in url or "xhslink.com" in url):
            state["card_url"] = url
            state["card_title"] = title
            return True

    # 2. 尝试文本消息
    if any(seg.type == "text" and seg.data.get("text", "").strip() for seg in event.message):
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
        _log_matcher_event("Douyin", event, success=False, message="未提取到视频ID")
        await douyin_matcher.finish("未找到有效的视频链接")

    _log_matcher_event("Douyin", event, video_id)

    try:
        # 1. 获取视频信息
        video_info = await douyin.get_video_info(video_id)
        if isinstance(video_info, str):
            # 记录详细错误到日志
            logger.warning(f"抖音视频获取失败: {video_info}")
            # 向用户发送详细错误信息
            await douyin_matcher.finish(video_info)

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


async def _process_xiaohongshu_url(jump_url: str) -> str:
    """处理小红书URL，包括短链接解析和参数提取（内部函数）"""
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
            logger.warning(f"小红书短链接解析异常: {jump_url} - {e}", exc_info=True)
            return ""

        # 使用工具函数解析短链接
        try:
            jump_url = await get_redirect_url(jump_url, timeout=10.0)
        except Exception as e:
            logger.warning(f"小红书短链接重定向失败: {jump_url} - {e}", exc_info=True)
            return ""

    # 提取笔记ID
    pattern = r"(?:/explore/|/discovery/item/|source=note&noteId=)(\w+)"
    if not (matched := re.search(pattern, jump_url)):
        # 如果无法提取ID，回退到原来的方法
        return await xiaohongshu.extract_url(jump_url)

    xhs_id = matched[1]
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


async def _download_single_image(pic_url: str) -> MessageSegment | None:
    """下载单张图片（内部函数）

    Args:
        pic_url: 图片URL

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
        logger.warning(f"下载图片失败 {pic_url}: {e}", exc_info=True)
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
            return await _download_single_image(url)

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
    # 添加文字内容节点
    forward_nodes: list[dict[str, Any]] = [
        {"type": "node", "data": {"name": "", "uin": bot.self_id, "content": info_text}}
    ]

    # 添加媒体内容节点
    if media_segments:
        forward_nodes.extend(
            {"type": "node", "data": {"name": "", "uin": bot.self_id, "content": seg}}
            for seg in media_segments
        )

    return forward_nodes


@xiaohongshu_matcher.handle()
async def handle_xiaohongshu_message(
    bot: Bot,
    event: MessageEvent,
    state: T_State,
):
    """处理小红书消息"""
    # 处理阶段检查 cookie 配置
    if not config.xiaohongshu_cookie:
        await xiaohongshu_matcher.finish("未配置小红书 cookie，无法解析笔记内容")

    url = ""

    # 优先使用检查阶段缓存的卡片URL（避免重复解析）
    if card_url := state.get("card_url"):
        url = await _process_xiaohongshu_url(card_url)

    # 回退到纯文本提取
    if not url:
        message = str(event.message).strip()
        url = await xiaohongshu.extract_url(message)

    if not url:
        _log_matcher_event("Xiaohongshu", event, success=False, message="未提取到笔记链接")
        await xiaohongshu_matcher.finish("未找到有效的笔记链接")

    _log_matcher_event("Xiaohongshu", event, f"{url[:50]}...", id_type="URL")

    try:
        # 1. 获取笔记信息
        note_info = await xiaohongshu.get_note_info(url)
        if isinstance(note_info, str):
            # 记录详细错误到日志
            logger.warning(f"小红书笔记获取失败: {note_info}")
            # 向用户发送详细错误信息
            await xiaohongshu_matcher.finish(note_info)

        info_text = f"{note_info['title']}\n作者: {note_info['author']}"

        # 2. 根据内容类型处理
        media_segments: list[MessageSegment] = []
        if note_info["pic_urls"]:
            # 处理图片内容 - 使用并发下载提升性能
            pic_urls = note_info["pic_urls"][:9]  # 最多处理9张图片
            logger.info(f"图片数量{len(pic_urls)}张，使用并发下载（max_concurrent=5）")
            media_segments = await download_images_concurrent(pic_urls, max_concurrent=5)
        elif note_info["video_url"]:
            # 处理视频内容
            video_data = await download_media(note_info["video_url"])
            media_segments = [MessageSegment.video(video_data)]

        # 统一发送转发消息
        forward_nodes = create_forward_nodes(bot, info_text, media_segments or None)
        await send_forward_message(bot, event, forward_nodes)

    except MatcherException:
        raise
    except Exception as e:
        # 记录详细错误到日志（包含堆栈）
        logger.error(f"处理小红书笔记失败: {e}", exc_info=True)
        # 向用户发送友好提示
        await xiaohongshu_matcher.finish("内容处理失败，请稍后重试")
