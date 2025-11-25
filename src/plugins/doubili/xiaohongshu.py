import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from httpx import AsyncClient
from nonebot import get_plugin_config, logger

from .config import Config

config = get_plugin_config(Config)


@dataclass
class ParseResult:
    """解析结果"""

    title: str
    cover_url: str
    video_url: str = ""
    pic_urls: list[str] = None
    author: str = ""


# 匹配小红书链接的模式
PATTERNS = {
    "xiaohongshu": re.compile(
        r"https?://(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/[A-Za-z\d._?%&+\-=/#]*"
    ),
    "xhslink": re.compile(r"https?://xhslink\.com/[A-Za-z\d._?%&+\-=/#]*"),
}

# 小红书专用请求头
XHS_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,"
    "application/signed-exchange;v=b3;q=0.9",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


class XiaoHongShuParser:
    """小红书解析器"""

    def __init__(self):
        self.headers = XHS_HEADERS.copy()
        # 如果配置了cookie，添加到请求头中
        if config.xiaohongshu_cookie:
            self.headers["cookie"] = config.xiaohongshu_cookie

    async def _get_redirect_url(self, url: str) -> str:
        """获取重定向后的URL"""
        async with AsyncClient() as client:
            response = await client.get(url, headers=self.headers, follow_redirects=True)
            return str(response.url)

    def _extract_note_id(self, url: str) -> str:
        """从URL中提取笔记ID"""
        # 匹配各种小红书URL格式中的笔记ID
        pattern = r"(?:/explore/|/discovery/item/|source=note&noteId=)(\w+)"
        if matched := re.search(pattern, url):
            return matched[1]
        raise ValueError("无法从URL中提取笔记ID")

    def _build_api_url(
        self, note_id: str, xsec_source: str = "pc_feed", xsec_token: str = ""
    ) -> str:
        """构建API请求URL"""
        base_url = f"https://www.xiaohongshu.com/explore/{note_id}"
        params = {"xsec_source": xsec_source}
        if xsec_token:
            params["xsec_token"] = xsec_token

        param_str = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{base_url}?{param_str}"

    def _parse_initial_state(self, html: str) -> dict[str, Any]:
        """解析页面中的初始状态数据"""
        pattern = r"window\.__INITIAL_STATE__=(.*?)</script>"
        if not (matched := re.search(pattern, html)):
            raise ValueError("页面中未找到初始状态数据")

        json_str = matched[1]
        # 处理JavaScript中的undefined值
        json_str = json_str.replace("undefined", "null")

        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ValueError(f"解析JSON数据失败: {e}") from e

    def _extract_note_data(self, json_obj: dict[str, Any], note_id: str) -> dict[str, Any]:
        """从JSON对象中提取笔记数据"""
        try:
            return json_obj["note"]["noteDetailMap"][note_id]["note"]
        except KeyError as e:
            logger.error(f"小红书数据结构解析失败: {e}", exc_info=True)
            raise ValueError("笔记数据不存在或结构异常") from e

    def _parse_media_content(self, note_data: dict[str, Any]) -> tuple[list[str], str]:
        """解析媒体内容（图片或视频）"""
        resource_type = note_data.get("type")
        img_urls = []
        video_url = ""

        if resource_type == "normal":
            # 图文类型
            image_list = note_data.get("imageList", [])
            img_urls = [item.get("urlDefault", "") for item in image_list if item.get("urlDefault")]
        elif resource_type == "video":
            # 视频类型
            video_data = note_data.get("video", {})
            stream = video_data.get("media", {}).get("stream", {})

            # 按优先级尝试不同编码格式
            for codec in ("h264", "h265", "av1"):
                if stream.get(codec):
                    video_url = stream[codec][0].get("masterUrl", "")
                    if video_url:
                        break

            if not video_url:
                raise ValueError("无法获取视频播放地址")
        else:
            raise ValueError(f"不支持的内容类型: {resource_type}")

        return img_urls, video_url

    async def parse_url(self, url: str) -> ParseResult:
        """解析小红书URL

        Args:
            url: 小红书分享链接

        Returns:
            ParseResult: 解析结果

        Raises:
            ValueError: URL格式错误或解析失败
        """
        try:
            # 处理短链接重定向
            if "xhslink" in url:
                url = await self._get_redirect_url(url)

            # 提取笔记ID
            note_id = self._extract_note_id(url)

            # 解析URL参数
            parsed_url = urlparse(url)
            params = parse_qs(parsed_url.query)
            xsec_source = params.get("xsec_source", ["pc_feed"])[0]
            xsec_token = params.get("xsec_token", [""])[0]

            # 构建请求URL
            api_url = self._build_api_url(note_id, xsec_source, xsec_token)

            # 发送请求获取页面内容
            async with AsyncClient() as client:
                response = await client.get(api_url, headers=self.headers, timeout=30.0)
                response.raise_for_status()
                html = response.text

            # 解析页面数据
            json_obj = self._parse_initial_state(html)
            note_data = self._extract_note_data(json_obj, note_id)

            # 提取基本信息
            title = note_data.get("title", "")
            desc = note_data.get("desc", "")
            title_desc = f"{title}\n{desc}" if title and desc else title or desc

            author = note_data.get("user", {}).get("nickname", "")

            # 解析媒体内容
            img_urls, video_url = self._parse_media_content(note_data)

            return ParseResult(
                title=title_desc,
                cover_url="",
                video_url=video_url,
                pic_urls=img_urls,
                author=author,
            )

        except Exception as e:
            logger.error(f"小红书解析失败: {e}", exc_info=True)
            raise ValueError(f"解析失败: {e}") from e


async def extract_url(text: str) -> str:
    """从文本中提取小红书URL"""
    for pattern in PATTERNS.values():
        if matched := pattern.search(text):
            return matched[0]
    return ""


# 创建解析器实例
xiaohongshu_parser = XiaoHongShuParser()


async def get_note_info(url: str) -> dict | str:
    """获取小红书笔记信息

    Args:
        url: 小红书链接

    Returns:
        解析结果字典或错误信息
    """
    try:
        result = await xiaohongshu_parser.parse_url(url)
        return {
            "title": result.title,
            "author": result.author,
            "video_url": result.video_url,
            "pic_urls": result.pic_urls or [],
            "cover_url": result.cover_url,
        }
    except Exception as e:
        logger.error(f"获取小红书笔记信息失败: {e}", exc_info=True)
        return f"获取笔记信息失败: {e}"
