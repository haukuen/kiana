import json
import re
from dataclasses import dataclass
from typing import Any

from httpx import AsyncClient
from nonebot import logger


@dataclass
class ParseResult:
    """解析结果"""

    title: str
    cover_url: str
    video_url: str = ""
    pic_urls: list[str] = None
    dynamic_urls: list[str] = None
    author: str = ""


IOS_HEADER = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1",
}

ANDROID_HEADER = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 11; Redmi K30 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.72 Mobile Safari/537.36",
}

# 匹配抖音链接的模式
PATTERNS = {
    "douyin": re.compile(
        r"https?://(?:v\.douyin\.com/[A-Za-z\d_-]+|www\.douyin\.com/(?:video|note)/\d+)"
    ),
}


class DouyinParser:
    def __init__(self):
        self.ios_headers = IOS_HEADER.copy()
        self.android_headers = {"Accept": "application/json, text/plain, */*", **ANDROID_HEADER}

    def _build_iesdouyin_url(self, _type: str, video_id: str) -> str:
        return f"https://www.iesdouyin.com/share/{_type}/{video_id}"

    def _build_m_douyin_url(self, _type: str, video_id: str) -> str:
        return f"https://m.douyin.com/share/{_type}/{video_id}"

    async def get_video_info(self, video_id: str) -> dict | str:
        """获取抖音视频信息"""
        try:
            share_url = f"https://www.douyin.com/video/{video_id}"
            video_info = await self.parse_share_url(share_url)

            return {
                "url": video_info.video_url,
                "headers": self.ios_headers,
                "title": video_info.title,
            }
        except Exception as e:
            logger.error(f"解析抖音视频失败: {e}", exc_info=True)
            return f"获取视频信息失败: {e!s}"

    async def parse_video(self, url: str) -> ParseResult:
        async with AsyncClient() as client:
            response = await client.get(url, headers=self.ios_headers)
            response.raise_for_status()
            text = response.text

        data: dict[str, Any] = self._format_response(text)

        # 获取视频播放地址
        video_url: str = data["video"]["play_addr"]["url_list"][0].replace("playwm", "play")
        if video_url:
            video_url = await get_redirect_url(video_url)

        return ParseResult(
            title=data["desc"],
            cover_url=data["video"]["cover"]["url_list"][0],
            video_url=video_url,
            author=data["author"]["nickname"],
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
                raise ValueError(f"无法从 {share_url} 中解析出 ID")
            _type, video_id = matched[1], matched[2]
            if _type == "slides":
                return await self.parse_slides(video_id)

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
        raise RuntimeError("作品已删除，或资源直链获取失败, 请稍后再试")

    async def parse_slides(self, video_id: str) -> ParseResult:
        """解析多视频链接（如：视频合集、直播回放等）"""
        try:
            url = self._build_m_douyin_url("video", video_id)
            async with AsyncClient() as client:
                response = await client.get(url, headers=self.ios_headers)
                response.raise_for_status()
                text = response.text

            data: dict[str, Any] = self._format_response(text)

            # 获取视频播放地址
            video_url: str = data["video"]["play_addr"]["url_list"][0].replace("playwm", "play")
            if video_url:
                video_url = await get_redirect_url(video_url)

            return ParseResult(
                title=data["desc"],
                cover_url=data["video"]["cover"]["url_list"][0],
                video_url=video_url,
                author=data["author"]["nickname"],
            )
        except Exception as e:
            logger.error(f"解析抖音视频失败: {e}", exc_info=True)
            raise e


async def get_redirect_url(url: str) -> str:
    """获取重定向后的URL"""
    async with AsyncClient() as client:
        response = await client.get(url, headers=IOS_HEADER, follow_redirects=True)
        return str(response.url)


async def extract_video_id(text: str) -> str:
    """从文本中提取视频ID"""
    if matched := PATTERNS["douyin"].search(text):
        share_url = matched[0]

        # 如果是短链接，先获取重定向后的URL
        if "v.douyin.com" in share_url:
            share_url = await get_redirect_url(share_url)

        # 从URL中提取视频ID
        if video_match := re.search(r"video/(\d+)", share_url):
            return video_match[1]

    return ""


# 创建解析器实例
douyin_parser = DouyinParser()


async def get_video_info(video_id: str) -> dict | str:
    """获取抖音视频信息"""
    logger.info(f"尝试获取抖音视频信息: {video_id}")
    try:
        share_url = f"https://www.douyin.com/video/{video_id}"
        video_info = await douyin_parser.parse_share_url(share_url)
        logger.info(f"获取到视频信息: {video_info.video_url}")

        return {"url": video_info.video_url, "headers": IOS_HEADER, "title": video_info.title}
    except Exception as e:
        logger.error(f"解析抖音视频失败: {e}", exc_info=True)
        return f"获取视频信息失败: {e!s}"
