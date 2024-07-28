from nonebot import get_plugin_config, on_command
from nonebot.plugin import PluginMetadata
from nonebot.typing import T_State
from nonebot.adapters import Bot, Event, Message
from nonebot.params import CommandArg
from nonebot.adapters.qq.message import Message as QQMessage
from httpx import AsyncClient
from nonebot import logger
from .status_code_handler import handle_status_code
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="nonebot-character-trace",
    description="",
    usage="",
    config=Config,
)

config = get_plugin_config(Config)

url = config.api

character_trace = on_command("识图", aliases={"识别","搜图"}, priority=10, block=True)

@character_trace.handle()
async def _(state: T_State, msg: Message = CommandArg()):
    if text := msg.get("text"):
        state["text"] = text
    else:
        state["text"] = None
    if url := msg.get("image"):
        state["image"] = url
    else:
        state["image"] = None

@character_trace.got("text")
@character_trace.got("image", prompt="请跟随消息发送图片")
async def _(bot: Bot, event: Event, state: T_State):
    if not state["image"]:
        await character_trace.reject('请跟随消息发送图片')

    image_url = state["image"]
    image_data = await process_image(image_url, state["text"])
    await character_trace.finish(image_data)


async def process_image(image_message: QQMessage, text):
    # 从 Message 对象中提取 URL
    image_url = image_message[0].data.get('url')
    #logger.info(f"image_message: {image_message}")
    #logger.info(f"识别图片: {image_url}")
    if not image_url:
        return "无法获取图片 URL"

    try:
        # 下载图片
        async with AsyncClient(trust_env=False) as client:
            logger.info(f"下载图片: {image_url}")
            response = await client.get(image_url)
            if response.is_error:
                return "获取图片失败"
            
            image_content = response.content

        # 如果text里有gal,搜索gal
        if text and "gal" in text[0].data.get('text'):
            async with AsyncClient(trust_env=False) as client:
                response = await client.post(
                    url, 
                    files={"image": ("image.png", image_content, "image/png")},
                    data={'model': config.high_gal}
                )
        else:
            # 搜索动漫人物 
            async with AsyncClient(trust_env=False) as client:
                response = await client.post(
                    url, 
                    files={"image": ("image.png", image_content, "image/png")},
                    data={'model': config.high_anime1}
                )
            
        # 处理接口返回的结果
        if response.is_error:
            logger.error(f"API 请求失败: {response.status_code}")
            return f"API 请求失败: {response.status_code}"

        result = response.json()
        status_description, http_status_code = handle_status_code(result["new_code"])

        logger.info(f"result: {result}")
        # 解析结果
        if result["new_code"] == 17720 and result["data"]:
            character_info = result["data"][0]
            
            name = character_info["name"]
            anime = character_info["cartoonname"]
            confidence = character_info["acc_percent"]
            # box = character_info["box"]
            
            # 构造返回消息
            message = "识别结果：\n"
            message += f"角色：{name}\n"
            message += f"出自：{anime}\n"
            message += f"置信度：{confidence*100:.2f}%\n"
            # message += f"位置：左上({box[0]:.2f}, {box[1]:.2f}), 右下({box[2]:.2f}, {box[3]:.2f})"
            if confidence < 0.6:
                message += "\n注意：置信度过低，识别结果仅供参考，请自行判断是否正确"
            
            return message
        else:
            logger.error(f"{status_description}(HTTP 状态码: {http_status_code})")
            return f"{status_description}(HTTP 状态码: {http_status_code})"
    
    except Exception as e:
        logger.error(f"处理图片时发生错误: {str(e)}")
        return f"处理过程中发生错误: {str(e)}"

