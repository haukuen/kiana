import re
from io import BytesIO
from typing import Dict, Optional, Tuple

from httpx import AsyncClient
from nonebot import get_plugin_config, logger

from .config import Config

config = get_plugin_config(Config)

# 匹配模式
PATTERNS = {
    "BV": re.compile(r"(BV[1-9a-zA-Z]{10})(?:\s)?(\d{1,3})?"),
    "av": re.compile(r"av(\d{6,})(?:\s)?(\d{1,3})?"),
    "/BV": re.compile(r"/(BV[1-9a-zA-Z]{10})"),
    "/av": re.compile(r"/av(\d{6,})"),
    "b23": re.compile(r"https?://b23\.tv/[A-Za-z\d\._?%&+\-=/#]+"),
    "bili2233": re.compile(r"https?://bili2233\.cn/[A-Za-z\d\._?%&+\-=/#]+"),
    "bilibili": re.compile(r"https?://(?:www|m)?\.?bilibili\.com/video/[A-Za-z\d\._?%&+\-=/#]+"),
}

async def get_redirect_url(url: str, headers: dict) -> str:
    """获取重定向后的URL"""
    async with AsyncClient() as client:
        response = await client.get(url, headers=headers, follow_redirects=True)
        return str(response.url)

async def extract_video_id(text: str) -> tuple[str, str]:
    """从文本中提取视频ID"""
    # 遍历所有模式进行匹配
    for key, pattern in PATTERNS.items():
        if matched := pattern.search(text):
            # 如果是短链接，先获取重定向后的URL
            if key in ("b23", "bili2233"):
                url = await get_redirect_url(matched.group(0), config.API_HEADERS)
                return await extract_video_id(url)

            # BV号直接提取
            if key in ("BV", "/BV"):
                return "BV", matched.group(1)
            # av号转换为aid
            if key in ("av", "/av"):
                return "aid", matched.group(1)
            # 网页链接提取BV号
            if key == "bilibili":
                bv_match = re.search(r"BV[1-9a-zA-Z]{10}", matched.group(0))
                if bv_match:
                    return "BV", bv_match.group(0)
                av_match = re.search(r"av(\d+)", matched.group(0))
                if av_match:
                    return "aid", av_match.group(1)

    return "", ""

async def get_video_info(bvid: str = None, aid: int = None):
    """获取 Bilibili 视频详细信息"""
    if not bvid and not aid:
        return "必须提供 bvid 或 aid 参数！"

    params = {"bvid": bvid, "aid": aid}

    async with AsyncClient() as client:
        response = await client.get(
            config.BILIBILI_VIEW_API_URL, headers=config.API_HEADERS, params=params
        )
        response.raise_for_status()
        data = response.json()

        if data.get("code") != 0:
            return f"获取视频信息失败：{data.get('message', '未知错误')}"

        if data["data"]["duration"] > config.MAX_VIDEO_DURATION:
            return f"视频时长超过{config.MAX_VIDEO_DURATION / 60:.1f}分钟，无法下载"

        return data["data"]

async def get_video_stream(bvid: str = None, aid: int = None) -> Dict | str:
    """获取 Bilibili 视频流信息"""
    video_info = await get_video_info(bvid=bvid, aid=aid)
    if isinstance(video_info, str):  # 如果返回的是错误信息
        return video_info

    cid = video_info.get("cid")
    if not cid:
        return "未能获取视频的 cid！"

    params = {
        "bvid": bvid,
        "cid": cid,
        "qn": config.VIDEO_QUALITY,
    }

    async with AsyncClient() as client:
        response = await client.get(
            config.BILIBILI_API_URL, headers=config.API_HEADERS, params=params
        )
        response.raise_for_status()
        data = response.json()

        if data.get("code") != 0:
            return f"获取视频信息失败：{data.get('message', '未知错误')}"

        video_url = data["data"]["durl"][0]["url"]
        video_size = int(data["data"]["durl"][0]["size"])

        if video_size > config.MAX_VIDEO_SIZE:
            return f"视频大小超过{config.MAX_VIDEO_SIZE / 1024 / 1024:.1f}MB，无法下载"

        return {"url": video_url, "headers": config.API_HEADERS}
