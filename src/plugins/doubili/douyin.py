import json
import re
from io import BytesIO
from typing import Dict, Optional

from httpx import AsyncClient
from nonebot import logger


class DouyinParser:
    ANDROID_HEADER = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.141 Mobile Safari/537.36",
    }
    
    IOS_HEADER = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_3 like Mac OS X) "
                     "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.2 Mobile/15E148 Safari/604.1",
    }

    def __init__(self):
        self.ios_headers = self.IOS_HEADER.copy()
        self.android_headers = {"Accept": "application/json, text/plain, */*", **self.ANDROID_HEADER}

    async def extract_video_id(self, url: str) -> Optional[str]:
        """从分享链接中提取视频ID"""
        if matched := re.match(r"https?://(?:v\.douyin\.com|www\.douyin\.com)/([A-Za-z0-9]+)", url):
            share_id = matched.group(1)
            try:
                async with AsyncClient(follow_redirects=True) as client:
                    response = await client.get(f"https://v.douyin.com/{share_id}/")
                    final_url = str(response.url)
                    video_id = re.search(r"video/(\d+)", final_url)
                    if video_id:
                        return video_id.group(1)
            except Exception as e:
                logger.error(f"解析抖音链接失败: {e}")
        return None

    async def get_video_data(self, video_id: str) -> Dict:
        """获取视频数据"""
        url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={video_id}"
        
        async with AsyncClient() as client:
            response = await client.get(url, headers=self.android_headers)
            response.raise_for_status()
            data = response.json()
            
            if not data.get("item_list"):
                raise ValueError("未找到视频信息")
                
            return data["item_list"][0]

    async def get_video_url(self, video_id: str) -> tuple[str, str, str]:
        try:
            data = await self.get_video_data(video_id)
            video_info = data["video"]
            
            # 获取无水印视频地址
            video_url = video_info["play_addr"]["url_list"][0].replace("playwm", "play")
            
            # 获取视频标题和封面
            title = data.get("desc", "未知标题")
            cover_url = video_info["cover"]["url_list"][0]
            
            # 获取重定向后的真实地址
            async with AsyncClient(follow_redirects=True) as client:
                response = await client.get(video_url)
                real_video_url = str(response.url)
                
            return real_video_url, title, cover_url
            
        except Exception as e:
            logger.error(f"获取抖音视频信息失败: {e}")
            raise

    async def download_video(self, video_url: str) -> BytesIO:
        """下载视频到内存"""
        async with AsyncClient() as client:
            response = await client.get(video_url)
            response.raise_for_status()
            return BytesIO(response.content)
