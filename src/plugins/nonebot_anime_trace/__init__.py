from nonebot import get_plugin_config, on_command
from nonebot.plugin import PluginMetadata
from nonebot.typing import T_State
from nonebot.adapters import Bot, Event, Message
from nonebot.params import CommandArg
from nonebot.adapters.qq.message import Message as QQMessage
from nonebot.adapters.qq import MessageSegment, ActionFailed
from httpx import AsyncClient
from nonebot import logger
import langid

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="nonebot-anime-trace",
    description="",
    usage="",
    config=Config,
)

config = get_plugin_config(Config)

anime_trace = on_command("搜番", aliases={"以图搜番"}, priority=10, block=True)

@anime_trace.handle()
async def _(state: T_State, msg: Message = CommandArg()):
    if text := msg.get("text"):
        state["text"] = text
    if url := msg.get("image"):
        state["image"] = url
    else:
        state["image"] = None
    
    
@anime_trace.got("image", prompt="请跟随消息发送图片")
async def _(bot: Bot, event: Event, state: T_State):
    if not state["image"]:
        await anime_trace.finish('请跟随消息发送图片')

    image_url = state["image"]
    
    data = await process_image(image_url)
    await bot.send(event,data["message"])

    if 'video_url' in data and data['video_url'] != '':
        video_url = data['video_url']
        logger.info(f'尝试发送视频:{video_url}')
        await bot.send(event, MessageSegment.video(video_url))
                


async def process_image(image_message: QQMessage):
    # 从 Message 对象中提取 URL
    image_url = image_message[0].data.get('url')

    if not image_url:
        return dict(message = "无法获取图片 URL")

    try:
        # 下载图片
        async with AsyncClient(trust_env=False) as client:
            response = await client.get(image_url)
            if response.is_error:
                logger.error("获取图片失败")
                dict(message = "获取图片失败")
            image_content = response.content

        # 上传图片到接口
        url = "https://api.trace.moe/search?anilistInfo&cutBorders"
        
        async with AsyncClient(trust_env=False) as client:
            response = await client.post(
                url, 
                headers={"User-Agent": "okhttp/4.9.3"},
                files={"image": ("image.png", image_content, "image/png")},
            )
            
        # 处理接口返回的结果
        if response.is_error:
            logger.error(f"API 请求失败: {response.status_code}")
            return dict(message = f"API 请求失败: {response.status_code}")
        
        result = response.json()
        # 解析结果
        if 'result' in result:
            # 必须带[0] 不然是list而不是dict
            first_result = result['result'][0]
            first_anilist = first_result['anilist']

            if 'synonyms' in first_anilist:
                name = await detect_simplified_chinese(first_anilist['synonyms'])
                if not name:
                    name = first_anilist['title']['native']
                else:
                    name = '、'.join(name)
            if 'from' in first_result:
                time_string = await convert_seconds_to_time(first_result["from"])
            else:
                time_string = ''
            message = "识别结果：\n"
            message += f"番名：{name}\n"
            message += f"第 {first_result['episode']} 集 {time_string}\n"
            message += f"置信度：{first_result['similarity']*100:.2f}%\n"


            
            video_url = first_result['video'] if 'video' in first_result else ''
            image_url = first_result['image'] if 'image' in first_result else ''

            return dict(message=message,video_url=video_url,image_url=image_url)
            
        else:
            return dict(message="未找到相关结果")
    
    except ActionFailed as e:
        logger.error(f"处理图片时发生错误: {e}")
        return dict(message=f"处理图片时发生错误: {e}")


async def convert_seconds_to_time(seconds):
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = int(seconds // 60)
    remaining_seconds = int(seconds % 60)
    return f"{minutes} 分 {remaining_seconds} 秒"


# 检测并保存简中名
async def detect_simplified_chinese(synonyms):
    simplified_chinese_synonyms = []
    for synonym in synonyms:
        lang, confidence = langid.classify(synonym)
        if lang == 'zh':
            simplified_chinese_synonyms.append(synonym)
    return simplified_chinese_synonyms