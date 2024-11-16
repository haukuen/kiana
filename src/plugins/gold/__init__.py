from nonebot import get_plugin_config
from nonebot.plugin import PluginMetadata
import http.client
import json
import time
from datetime import datetime
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
# 存储闭市期间的金价
closed_market_price = None
# 上次更新闭市金价的时间
last_closed_price_update = None

def is_market_closed():
    """判断当前是否为闭市时间"""
    now = datetime.now()
    weekday = now.weekday()  # 0-6: 周一到周日

    # 周六日和工作日九点前闭市
    if weekday in [5, 6]:
        return True
    
    return False

def should_update_closed_price():
    """判断是否需要更新闭市金价"""
    now = datetime.now()
    # 在周六0点更新闭市金价
    return now.weekday() == 5 and now.hour == 0

@gold.handle()
async def _(bot: Bot, event: Event):
    global cooldown_dict, closed_market_price, last_closed_price_update

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

    # 如果是闭市时间且已有闭市金价，直接返回
    if is_market_closed() and closed_market_price is not None:
        await gold.finish(f"{closed_market_price}（当前非交易时间，返回最后交易价格）")
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
            
            # 如果需要更新闭市金价，则更新
            if should_update_closed_price():
                closed_market_price = zBuyPrc
                last_closed_price_update = current_time
            
            # 如果是闭市时间，添加闭市提示
            if is_market_closed():
                await gold.finish(f"{zBuyPrc}（当前非交易时间，返回最后交易价格）")
            else:
                await gold.finish(f"{zBuyPrc}")
        else:
            await gold.finish("获取金价失败")
    except json.JSONDecodeError:
        await gold.finish("解析金价数据失败")
    except KeyError:
        await gold.finish("金价数据格式错误")