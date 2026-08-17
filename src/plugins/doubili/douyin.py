import json
import re
from dataclasses import dataclass
from typing import Any

from httpx import AsyncClient
from nonebot import get_plugin_config, logger

from .config import Config
from .exceptions import ParseError, VideoFetchError

config = get_plugin_config(Config)


@dataclass
class ParseResult:
    """解析结果"""

    title: str
    cover_url: str
    video_url: str = ""
    duration_ms: int = 0
    pic_urls: list[str] | None = None
    dynamic_urls: list[str] | None = None
    author: str = ""


IOS_HEADER = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.douyin.com/",
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1",
}

ANDROID_HEADER = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 11; Redmi K30 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.72 Mobile Safari/537.36",
}

WEB_HEADER = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36",
    "Referer": "https://www.douyin.com/",
}

TTWID_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
TTWID_BODY = (
    '{"region":"cn","aid":1768,"needFid":false,'
    '"service":"www.ixigua.com","migrate_info":{"ticket":"","source":"node"},'
    '"cbUrlProtocol":"https","union":true}'
)
AWEME_DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

# 匹配抖音链接的模式
PATTERNS = {
    "douyin": re.compile(
        r"https?://(?:v\.douyin\.com/[A-Za-z\d_-]+|www\.douyin\.com/(?:video|note)/\d+)"
    ),
}


async def get_ttwid() -> str:
    """获取抖音网页版请求所需的 ttwid cookie"""
    async with AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
        response = await client.post(TTWID_URL, content=TTWID_BODY)
        response.raise_for_status()
    ttwid = response.cookies.get("ttwid")
    if not ttwid:
        raise ValueError("获取 ttwid 失败")
    return ttwid


async def fetch_aweme_detail(video_id: str) -> dict[str, Any]:
    """通过抖音 Web API 获取作品详情

    Args:
        video_id: 作品 ID

    Returns:
        作品的 aweme_detail 字典

    Raises:
        ValueError: 请求失败或响应中缺少作品信息
    """
    params = {
        "aweme_id": video_id,
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "pc_client_type": "1",
        "version_code": "190500",
        "version_name": "19.5.0",
        "cookie_enabled": "true",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_online": "true",
        "engine_name": "Blink",
        "os_name": "Windows",
        "os_version": "10",
        "platform": "PC",
        "screen_width": "1920",
        "screen_height": "1080",
        "browser_version": "90.0.4430.212",
        "engine_version": "90.0.4430.212",
        "cpu_core_num": "12",
        "device_memory": "8",
    }
    headers = {**WEB_HEADER, "Cookie": f"ttwid={await get_ttwid()}"}

    async with AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
        response = await client.get(AWEME_DETAIL_URL, params=params, headers=headers)
        response.raise_for_status()
        if not response.text:
            raise ValueError("Web API 返回空响应")
        data = response.json()

    if data.get("status_code") != 0:
        raise ValueError(f"Web API 返回错误: {data.get('status_msg', '未知错误')}")
    if not data.get("aweme_detail"):
        raise ValueError("Web API 响应缺少 aweme_detail")
    return data["aweme_detail"]


class DouyinParser:
    def __init__(self):
        self.ios_headers = IOS_HEADER.copy()
        self.android_headers = {"Accept": "application/json, text/plain, */*", **ANDROID_HEADER}

    def _build_iesdouyin_url(self, _type: str, video_id: str) -> str:
        return f"https://www.iesdouyin.com/share/{_type}/{video_id}"

    def _build_m_douyin_url(self, _type: str, video_id: str) -> str:
        return f"https://m.douyin.com/share/{_type}/{video_id}"

    async def get_video_info(self, video_id: str) -> dict:
        """获取抖音视频信息

        Args:
            video_id: 视频 ID

        Returns:
            包含 url, headers, title 的字典

        Raises:
            VideoFetchError: 获取视频信息失败
        """
        try:
            share_url = f"https://www.douyin.com/video/{video_id}"
            video_info = await self.parse_share_url(share_url)

            return {
                "url": video_info.video_url,
                "headers": self.ios_headers,
                "title": video_info.title,
            }
        except VideoFetchError:
            raise
        except Exception as e:
            logger.error(f"解析抖音视频失败: {e}", exc_info=True)
            raise VideoFetchError(f"获取视频信息失败: {e!s}") from e

    async def parse_video(self, url: str) -> ParseResult:
        async with AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
            response = await client.get(url, headers=self.ios_headers)
            response.raise_for_status()
            text = response.text

        data: dict[str, Any] = self._format_response(text)
        return await self._parse_item(data)

    async def _parse_item(self, data: dict[str, Any]) -> ParseResult:
        """将作品数据转换为统一结果结构"""
        # 检查是否为图文内容
        images = data.get("images")
        if images:
            pic_urls = [img["url_list"][0] for img in images if img.get("url_list")]
            return ParseResult(
                title=data["desc"],
                cover_url=data["video"]["cover"]["url_list"][0]
                if data.get("video", {}).get("cover", {}).get("url_list")
                else "",
                pic_urls=pic_urls,
                author=data["author"]["nickname"],
            )

        # 获取视频播放地址
        video_url: str = data["video"]["play_addr"]["url_list"][0].replace("playwm", "play")
        if video_url:
            video_url = await get_redirect_url(video_url)

        return ParseResult(
            title=data["desc"],
            cover_url=data["video"]["cover"]["url_list"][0],
            video_url=video_url,
            author=data["author"]["nickname"],
            duration_ms=data.get("video", {}).get("duration", 0) or 0,
        )

    def _format_response(self, text: str) -> dict[str, Any]:
        pattern = re.compile(
            pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
            flags=re.DOTALL,
        )
        if not (find_res := pattern.search(text)) or not find_res[1]:
            raise ValueError("无法从页面提取视频信息")

        json_data = json.loads(find_res[1].strip())

        video_id_page_key = "video_(id)/page"
        note_id_page_key = "note_(id)/page"

        if video_id_page_key in json_data["loaderData"]:
            original_video_info = json_data["loaderData"][video_id_page_key]["videoInfoRes"]
        elif note_id_page_key in json_data["loaderData"]:
            original_video_info = json_data["loaderData"][note_id_page_key]["videoInfoRes"]
        else:
            raise ValueError("无法解析视频信息")

        if len(original_video_info["item_list"]) == 0:
            err_msg = "无法获取视频信息"
            if len(filter_list := original_video_info["filter_list"]) > 0:
                err_msg = filter_list[0]["detail_msg"] or filter_list[0]["filter_reason"]
            raise ValueError(err_msg)

        return original_video_info["item_list"][0]

    async def parse_share_url(self, share_url: str) -> ParseResult:
        if matched := re.match(r"(video|note)/([0-9]+)", share_url):
            _type, video_id = matched[1], matched[2]
            iesdouyin_url = self._build_iesdouyin_url(_type, video_id)
        else:
            iesdouyin_url = await get_redirect_url(share_url)
            if not (matched := re.search(r"(slides|video|note)/(\d+)", iesdouyin_url)):
                raise ParseError(f"无法从 {share_url} 中解析出 ID")
            _type, video_id = matched[1], matched[2]
            if _type == "slides":
                return await self.parse_slides(video_id)

        # 抖音页面已不再内嵌作品数据，优先使用 Web API
        try:
            data = await fetch_aweme_detail(video_id)
            return await self._parse_item(data)
        except Exception as e:
            logger.warning(f"Web API 解析失败 {video_id}: {e}")

        for url in [
            self._build_m_douyin_url(_type, video_id),
            share_url,
            iesdouyin_url,
        ]:
            try:
                return await self.parse_video(url)
            except Exception as e:
                logger.warning(f"解析失败 {url[:60]}, error: {e}", exc_info=True)
                continue
        raise VideoFetchError("作品已删除，或资源直链获取失败, 请稍后再试")

    async def parse_slides(self, video_id: str) -> ParseResult:
        """解析多视频链接（如：视频合集、直播回放等）"""
        try:
            try:
                data = await fetch_aweme_detail(video_id)
                return await self._parse_item(data)
            except Exception as e:
                logger.warning(f"Web API 解析 slides 失败 {video_id}: {e}")

            url = self._build_m_douyin_url("video", video_id)
            async with AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
                response = await client.get(url, headers=self.ios_headers)
                response.raise_for_status()
                text = response.text

            data = self._format_response(text)
            return await self._parse_item(data)
        except Exception as e:
            logger.error(f"解析抖音视频失败: {e}", exc_info=True)
            raise VideoFetchError(f"解析抖音视频失败: {e!s}") from e


async def get_redirect_url(url: str) -> str:
    """获取重定向后的URL"""
    async with AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
        response = await client.get(url, headers=IOS_HEADER, follow_redirects=True)
        return str(response.url)


async def extract_video_info(text: str) -> tuple[str, str]:
    """从文本中提取内容类型和ID

    Returns:
        (content_type, video_id) 元组，content_type 为 "video" 或 "note"，未找到返回 ("", "")
    """
    if matched := PATTERNS["douyin"].search(text):
        share_url = matched[0]

        # 如果是短链接，先获取重定向后的URL
        if "v.douyin.com" in share_url:
            share_url = await get_redirect_url(share_url)

        # 从URL中提取内容类型和ID
        if content_match := re.search(r"(video|note)/(\d+)", share_url):
            return (content_match[1], content_match[2])

    return ("", "")


# 创建解析器实例
douyin_parser = DouyinParser()


async def get_video_info(content_type: str, video_id: str) -> ParseResult:
    """获取抖音视频信息

    Args:
        content_type: 内容类型 ("video" 或 "note")
        video_id: 视频 ID

    Returns:
        ParseResult 包含视频或图文信息

    Raises:
        VideoFetchError: 获取视频信息失败
    """
    logger.info(f"尝试获取抖音视频信息: {content_type}/{video_id}")
    try:
        share_url = f"https://www.douyin.com/{content_type}/{video_id}"
        video_info = await douyin_parser.parse_share_url(share_url)
        logger.info(
            f"获取到视频信息: video_url={video_info.video_url}, "
            f"duration_ms={video_info.duration_ms}, pic_urls={video_info.pic_urls}"
        )

        return video_info
    except VideoFetchError:
        raise
    except Exception as e:
        logger.error(f"解析抖音视频失败: {e}", exc_info=True)
        raise VideoFetchError(f"获取视频信息失败: {e!s}") from e
