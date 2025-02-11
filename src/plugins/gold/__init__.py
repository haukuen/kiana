from nonebot import get_plugin_config
from nonebot.plugin import PluginMetadata
import http.client
import json
import time
from nonebot import on_fullmatch
from nonebot.adapters.onebot.v11 import Bot, Event

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="gold",
    description="",
    usage="",
    config=Config,
)

config = get_plugin_config(Config)

gold = on_fullmatch("金价")

# 存储冷却时间的字典，每个群单独冷却
cooldown_dict = {}

@gold.handle()
async def _(bot: Bot, event: Event):
    # 获取当前时间
    current_time = time.time()

    # 获取群号
    group_id = event.group_id

    # 检查是否在冷却时间内
    if cooldown_dict.get(group_id, {}).get("last_call_time", 0) + config.COOLDOWN_TIME > current_time:
        remaining_time = int(cooldown_dict[group_id]["last_call_time"] + config.COOLDOWN_TIME - current_time)
        if remaining_time == 0:
            remaining_time = 1
        await gold.finish(f"冷却中，请等待 {remaining_time} 秒后再试")
        return

    conn = http.client.HTTPSConnection("mbmodule-openapi.paas.cmbchina.com")
    payload = config.API_PAYLOAD
    headers = config.API_HEADERS
    conn.request("POST", config.API_URL, payload, headers)
    res = conn.getresponse()
    data = res.read()
    
    try:
        json_data = json.loads(data.decode("utf-8"))
        if json_data.get("success"):
            zBuyPrc = json_data["data"]["FQAMBPRCZ1"]["zBuyPrc"]
            # 更新冷却时间
            if group_id not in cooldown_dict:
                cooldown_dict[group_id] = {}
            cooldown_dict[group_id]["last_call_time"] = current_time
            
            await gold.finish(f"{zBuyPrc}")
        else:
            await gold.finish("获取金价失败")
    except json.JSONDecodeError:
        await gold.finish("解析金价数据失败")
    except KeyError:
        await gold.finish("金价数据格式错误")