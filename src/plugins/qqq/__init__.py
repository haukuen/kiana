"""QQQ（纳斯达克 100 ETF）行情查询插件"""

import time
from dataclasses import dataclass

import httpx
from nonebot import get_plugin_config, logger, on_fullmatch
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent
from nonebot.plugin import PluginMetadata

from ..group_permission import create_sub_feature_rule
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="qqq",
    description="QQQ（纳斯达克 100 ETF）实时行情查询",
    usage="发送 qqq 查询最新行情",
    config=Config,
)

SINA_QUOTE_URL = "https://hq.sinajs.cn/list=gb_qqq"
SINA_REFERER = "https://finance.sina.com.cn"
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q=usQQQ"
REQUEST_TIMEOUT = 5.0
DISPLAY_NAME = "纳斯达克100ETF"

config: Config = get_plugin_config(Config)

is_qqq_query_enabled = create_sub_feature_rule(
    config_getter=lambda: config,
    plugin_enabled_attr="qqq_plugin_enabled",
    feature_enabled_attr="qqq_enable_price_query",
    prefix="qqq_",
)

qqq = on_fullmatch("qqq", rule=is_qqq_query_enabled, ignorecase=True, priority=5, block=True)


@dataclass
class QQQQuote:
    """QQQ 行情数据"""

    name: str
    price: float
    change: float
    percent: float
    open: float
    high: float
    low: float
    prev_close: float

    def format_message(self) -> str:
        pct_sign = "+" if self.percent >= 0 else ""
        return f"{self.price:.2f}（{pct_sign}{self.percent:.2f}%）"


class CooldownManager:
    """群聊查询冷却管理器"""

    def __init__(self) -> None:
        self._last_call: dict[int, float] = {}

    def remaining(self, group_id: int, cooldown_time: int) -> int:
        last_call = self._last_call.get(group_id, 0)
        return max(0, int(last_call + cooldown_time - time.time()))

    def update(self, group_id: int) -> None:
        self._last_call[group_id] = time.time()


cooldown_manager = CooldownManager()


def _parse_sina_quote(content: bytes) -> QQQQuote | None:
    """解析新浪行情返回（GBK 编码的逗号分隔文本）"""
    try:
        text = content.decode("gbk", errors="replace")
        body = text.split('="', 1)[1].strip('";')
        fields = body.split(",")
        if len(fields) < 27 or not fields[1]:
            return None
        return QQQQuote(
            name=DISPLAY_NAME,
            price=float(fields[1]),
            change=float(fields[4]),
            percent=float(fields[2]),
            open=float(fields[5]),
            high=float(fields[6]),
            low=float(fields[7]),
            prev_close=float(fields[26]),
        )
    except (IndexError, ValueError):
        return None


def _parse_tencent_quote(content: bytes) -> QQQQuote | None:
    """解析腾讯行情返回（GBK 编码的波浪号分隔文本）"""
    try:
        text = content.decode("gbk", errors="replace")
        body = text.split('="', 1)[1].strip('";')
        fields = body.split("~")
        if len(fields) < 35 or not fields[3]:
            return None
        return QQQQuote(
            name=DISPLAY_NAME,
            price=float(fields[3]),
            change=float(fields[31]),
            percent=float(fields[32]),
            open=float(fields[5]),
            high=float(fields[33]),
            low=float(fields[34]),
            prev_close=float(fields[4]),
        )
    except (IndexError, ValueError):
        return None


async def fetch_qqq_quote() -> QQQQuote | None:
    """获取 QQQ 行情，优先新浪接口，失败时回退腾讯接口"""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            response = await client.get(SINA_QUOTE_URL, headers={"Referer": SINA_REFERER})
            response.raise_for_status()
            quote = _parse_sina_quote(response.content)
            if quote is not None:
                return quote
            logger.warning("新浪行情解析失败，回退腾讯接口")
        except httpx.HTTPError as e:
            logger.warning(f"新浪行情请求失败: {e}")

        try:
            response = await client.get(TENCENT_QUOTE_URL)
            response.raise_for_status()
            quote = _parse_tencent_quote(response.content)
            if quote is not None:
                return quote
            logger.warning("腾讯行情解析失败")
        except httpx.HTTPError as e:
            logger.warning(f"腾讯行情请求失败: {e}")

    return None


@qqq.handle()
async def handle_group_qqq_query(event: GroupMessageEvent) -> None:
    """处理群聊 QQQ 查询（带冷却机制）"""
    remaining_time = cooldown_manager.remaining(event.group_id, config.qqq_cooldown_time)
    if remaining_time > 0:
        await qqq.finish(f"冷却中，请等待 {remaining_time} 秒后再试")

    quote = await fetch_qqq_quote()
    if quote is None:
        await qqq.finish("获取 QQQ 行情失败，请稍后重试")

    cooldown_manager.update(event.group_id)
    await qqq.finish(quote.format_message())


@qqq.handle()
async def handle_private_qqq_query(event: PrivateMessageEvent) -> None:
    """处理私聊 QQQ 查询（无冷却限制）"""
    quote = await fetch_qqq_quote()
    if quote is None:
        await qqq.finish("获取 QQQ 行情失败，请稍后重试")

    await qqq.finish(quote.format_message())
