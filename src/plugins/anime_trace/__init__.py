from nonebot import get_plugin_config, on_command
from nonebot.plugin import PluginMetadata
from nonebot.typing import T_State
from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment, MessageEvent, GroupMessageEvent
from nonebot.params import CommandArg
from httpx import AsyncClient, HTTPStatusError, RequestError
from nonebot import logger
import langid
import json
from nonebot.rule import Rule, to_me

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="nonebot-anime-trace",
    description="通过图片搜索动漫",
    usage="发送命令 '搜番' 或 '以图搜番' 并附带图片",
    config=Config,
)

config = get_plugin_config(Config)

def has_image() -> Rule:
    async def _has_image(event: MessageEvent) -> bool:
        if event.reply:
            # 检查回复的消息中是否包含图片
            for seg in event.reply.message:
                if seg.type == "image":
                    return True
        return False
    return Rule(_has_image)

anime_trace = on_command("搜番", aliases={"以图搜番"}, priority=10, block=True)

@anime_trace.handle()
async def handle_anime_trace(bot: Bot, event: MessageEvent, state: T_State, msg: Message = CommandArg()):
    # 首先检查是否是回复消息
    if event.reply:
        for seg in event.reply.message:
            if seg.type == "image":
                state["image"] = seg.data["url"]
                break
    
    # 如果不是回复消息，检查命令中是否包含图片
    if "image" not in state and msg["image"]:
        state["image"] = msg["image"][0].data["url"]
    
    # 如果没有找到图片，提示用户
    if "image" not in state:
        await anime_trace.finish("请发送图片或回复包含图片的消息")
        return
    
    try:
        result = await process_image(state["image"])
        await bot.send(event, result["message"])

        if 'video_url' in result and result['video_url']:
            logger.info(f'尝试发送视频: {result["video_url"]}')
            await bot.send(event, MessageSegment.video(result["video_url"]))
    except Exception as e:
        logger.error(f"处理图片时发生错误: {e}", exc_info=True)
        await bot.send(event, f"处理图片时发生错误: {str(e)}")

async def process_image(image_url: str):
    if not image_url:
        raise ValueError("无法获取图片 URL")

    try:
        # 下载图片
        image_content = await download_image(image_url)
        
        # 上传图片到接口
        result = await upload_image_to_api(image_content)
        
        # 解析结果
        return parse_api_result(result)
    
    except HTTPStatusError as e:
        logger.error(f"HTTP错误: {e}", exc_info=True)
        raise Exception(f"API请求失败 (HTTP {e.response.status_code})")
    except RequestError as e:
        logger.error(f"请求错误: {e}", exc_info=True)
        raise Exception("网络请求失败，请检查网络连接")
    except json.JSONDecodeError as e:
        logger.error(f"JSON解析错误: {e}", exc_info=True)
        raise Exception("API返回的数据格式不正确")
    except Exception as e:
        logger.error(f"处理图片时发生未知错误: {e}", exc_info=True)
        raise Exception(f"处理图片时发生未知错误: {str(e)}")

async def download_image(image_url: str):
    logger.info(f"开始下载图片: {image_url}")
    async with AsyncClient(trust_env=False) as client:
        response = await client.get(image_url)
        response.raise_for_status()
        return response.content

async def upload_image_to_api(image_content):
    url = "https://api.trace.moe/search?anilistInfo&cutBorders"
    logger.info(f"开始上传图片到接口: {url}")
    
    async with AsyncClient(trust_env=False) as client:
        response = await client.post(
            url, 
            headers={"User-Agent": "okhttp/4.9.3"},
            files={"image": ("image.png", image_content, "image/png")},
            timeout=30.0  # 设置30秒超时
        )
        response.raise_for_status()
    
    return response.json()

def parse_api_result(result):
    logger.info(f"接口返回结果: {result}")

    if not result.get('result'):
        raise ValueError("API返回结果中没有找到 'result' 字段")

    first_result = result['result'][0]
    first_anilist = first_result['anilist']

    name = detect_simplified_chinese(first_anilist.get('synonyms', []))
    if not name:
        name = first_anilist['title'].get('native', '未知番名')
    else:
        name = '、'.join(name)

    time_string = convert_seconds_to_time(first_result.get("from", 0))

    message = "识别结果：\n"
    message += f"番名：{name}\n"
    message += f"第 {first_result.get('episode', '未知')} 集 {time_string}\n"
    message += f"置信度：{first_result['similarity']*100:.2f}%\n"

    if first_anilist.get('isAdult', False):
        message += "（注意：该内容可能不适合所有年龄段）"
        return dict(
            message=message,
            video_url=None,
            image_url=None
        )

    video_url = first_result.get('video', '')
    image_url = first_result.get('image', '')

    return dict(message=message, video_url=video_url, image_url=image_url)

def convert_seconds_to_time(seconds):
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = int(seconds // 60)
    remaining_seconds = int(seconds % 60)
    return f"{minutes} 分 {remaining_seconds} 秒"

def detect_simplified_chinese(synonyms):
    simplified_chinese_synonyms = []
    for synonym in synonyms:
        lang, confidence = langid.classify(synonym)
        if lang == 'zh':
            simplified_chinese_synonyms.append(synonym)
    return simplified_chinese_synonyms