import http.client
import io
import json
import time
from collections import deque
from datetime import datetime
from typing import Deque, Tuple

import matplotlib.pyplot as plt
from nonebot import get_plugin_config, logger, on_fullmatch, require
from nonebot.adapters.onebot.v11 import Bot, Event, MessageSegment
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="gold",
    description="",
    usage="",
    config=Config,
)

config = get_plugin_config(Config)

gold = on_fullmatch("金价")
gold_chart = on_fullmatch(("金价走势", "金价趋势", "黄金走势", "黄金趋势", "金价图", "黄金图"))

# 存储冷却时间的字典，每个群单独冷却
cooldown_dict = {}

# 存储最近24小时的金价数据 (时间戳, 价格)
price_history: Deque[Tuple[float, float]] = deque(maxlen=8640)  # 24小时 * 360条/小时

scheduler = require("nonebot_plugin_apscheduler").scheduler


async def fetch_gold_price() -> float | None:
    """获取金价"""
    try:
        conn = http.client.HTTPSConnection("mbmodule-openapi.paas.cmbchina.com")
        payload = config.API_PAYLOAD
        headers = config.API_HEADERS
        conn.request("POST", config.API_URL, payload, headers)
        res = conn.getresponse()
        data = res.read()

        json_data = json.loads(data.decode("utf-8"))
        if json_data.get("success"):
            return float(json_data["data"]["FQAMBPRCZ1"]["zBuyPrc"])
        return None
    except (http.client.HTTPException, json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"获取金价失败: {e}")
        return None


@scheduler.scheduled_job("interval", seconds=10)
async def record_price():
    """每10秒记录一次金价"""
    price = await fetch_gold_price()
    if price is not None:
        price_history.append((time.time(), price))


def generate_chart() -> bytes:
    """生成金价走势图"""
    plt.style.use("bmh")

    plt.figure(figsize=(12, 6))
    plt.clf()

    times, prices = zip(*list(price_history), strict=False)
    # 转换为本地时间
    times = [datetime.fromtimestamp(t).astimezone() for t in times]

    plt.plot(times, prices)
    plt.grid(True)

    # 自动调整x轴日期格式
    plt.gcf().autofmt_xdate()

    # 将图表保存到内存中
    buf = io.BytesIO()
    plt.savefig(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


@gold.handle()
async def _(bot: Bot, event: Event):
    # 获取当前时间
    current_time = time.time()

    # 获取群号
    group_id = event.group_id

    # 检查是否在冷却时间内
    if (
        cooldown_dict.get(group_id, {}).get("last_call_time", 0) + config.COOLDOWN_TIME
        > current_time
    ):
        remaining_time = int(
            cooldown_dict[group_id]["last_call_time"] + config.COOLDOWN_TIME - current_time
        )
        if remaining_time == 0:
            remaining_time = 1
        await gold.finish(f"冷却中，请等待 {remaining_time} 秒后再试")
        return

    price = await fetch_gold_price()
    if price is not None:
        # 更新冷却时间
        if group_id not in cooldown_dict:
            cooldown_dict[group_id] = {}
        cooldown_dict[group_id]["last_call_time"] = current_time

        await gold.finish(f"{price}")
    else:
        await gold.finish("获取金价失败")


@gold_chart.handle()
async def _(bot: Bot, event: Event):
    """处理金价走势图请求"""
    if len(price_history) < 2:
        await gold_chart.finish("数据收集中，请稍后再试")
        return

    try:
        image_data = generate_chart()
        await gold_chart.send(MessageSegment.image(image_data))
    except Exception as e:
        await gold_chart.send(f"生成图表失败: {str(e)}")
