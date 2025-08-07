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

require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as store  # noqa: E402

from .config import Config  # noqa: E402

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

price_history: deque[tuple[float, float]] = deque(maxlen=86400)

scheduler = require("nonebot_plugin_apscheduler").scheduler

PRICE_DATA_FILE = store.get_data_file("gold", "price_history.json")
driver = get_driver()

# 保存间隔时间
SAVE_INTERVAL = 300


class PriceManager:
    """价格数据管理器"""

    def __init__(self):
        self.last_save_time = 0

    def should_save(self, current_time: float) -> bool:
        """检查是否应该保存数据"""
        return current_time - self.last_save_time >= SAVE_INTERVAL

    def update_save_time(self, current_time: float) -> None:
        """更新最后保存时间"""
        self.last_save_time = current_time


# 创建价格管理器实例
price_manager = PriceManager()


def save_price_history() -> None:
    """保存价格历史到文件"""
    try:
        PRICE_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        with PRICE_DATA_FILE.open("w", encoding="utf-8") as f:
            # 转换为列表存储
            data = list(price_history)
            json.dump(data, f)
        logger.info(f"已保存 {len(price_history)} 条金价数据")
    except Exception as e:
        logger.error(f"保存金价数据失败: {e}")


def load_price_history() -> None:
    """从文件加载价格历史"""
    try:
        if PRICE_DATA_FILE.exists():
            with PRICE_DATA_FILE.open(encoding="utf-8") as f:
                data: list[tuple[float, float]] = json.load(f)
                price_history.clear()
                price_history.extend(data)
            logger.info(f"已加载 {len(data)} 条历史金价数据")
    except Exception as e:
        logger.error(f"加载历史金价数据失败: {e}")


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


@scheduler.scheduled_job("interval", seconds=config.price_fetch_interval)
async def record_price():
    """定时记录金价"""
    current_time = time.time()

    price = await fetch_gold_price()
    if price is not None:
        price_history.append((current_time, price))

        # 每隔 SAVE_INTERVAL 秒保存一次
        if price_manager.should_save(current_time):
            save_price_history()
            price_manager.update_save_time(current_time)


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
    load_price_history()


@driver.on_shutdown
async def _():
    save_price_history()
