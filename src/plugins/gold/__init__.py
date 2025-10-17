import http.client
import io
import json
import time
from collections import deque
from datetime import datetime

import matplotlib.pyplot as plt
from nonebot import get_driver, get_plugin_config, logger, on_fullmatch, require
from nonebot.adapters.onebot.v11 import Bot, Event, MessageSegment
from nonebot.plugin import PluginMetadata

from src.storage import get_db

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

PRICE_HISTORY_LIMIT = 86400
price_history: deque[tuple[float, float]] = deque()

scheduler = require("nonebot_plugin_apscheduler").scheduler
driver = get_driver()

db = get_db()
db.ensure_schema(
    [
        """
        CREATE TABLE IF NOT EXISTS gold_price_history (
            timestamp REAL PRIMARY KEY,
            price REAL NOT NULL
        )
        """
    ]
)


async def load_price_history() -> None:
    """从数据库加载最近的价格历史"""
    rows = await db.fetch_all(
        """
        SELECT timestamp, price
        FROM gold_price_history
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (PRICE_HISTORY_LIMIT,),
    )

    price_history.clear()
    for row in reversed(rows):
        price_history.append((row["timestamp"], row["price"]))

    if rows:
        logger.info(f"已从数据库加载 {len(rows)} 条历史金价数据")


async def persist_price(timestamp: float, price: float) -> None:
    """写入数据库并维护内存中的价格历史"""
    if len(price_history) >= PRICE_HISTORY_LIMIT:
        oldest_timestamp, _ = price_history.popleft()
        await db.execute(
            "DELETE FROM gold_price_history WHERE timestamp = ?",
            (oldest_timestamp,),
        )

    price_history.append((timestamp, price))
    await db.execute(
        """
        INSERT OR REPLACE INTO gold_price_history (timestamp, price)
        VALUES (?, ?)
        """,
        (timestamp, price),
    )


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
    except (OSError, http.client.HTTPException, json.JSONDecodeError, KeyError, ValueError) as e:
        logger.error(f"获取金价失败: {e}")
        return None


@scheduler.scheduled_job("interval", seconds=config.price_fetch_interval)
async def record_price():
    """定时记录金价"""
    current_time = time.time()

    price = await fetch_gold_price()
    if price is not None:
        await persist_price(current_time, price)


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
        cooldown_dict.get(group_id, {}).get("last_call_time", 0) + config.cooldown_time
        > current_time
    ):
        remaining_time = int(
            cooldown_dict[group_id]["last_call_time"] + config.cooldown_time - current_time
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
        await gold_chart.send(f"生成图表失败: {e!s}")


@driver.on_startup
async def _():
    await load_price_history()
