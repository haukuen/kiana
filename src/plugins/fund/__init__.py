import io
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import httpx
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from nonebot import logger, on_regex
from nonebot.adapters.onebot.v11 import Bot, Event, MessageSegment
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="fund",
    description="基金查询插件",
    usage="发送基金代码查询基金信息，如：016057",
)

fund_query = on_regex(r"^\d{6}$")


class FundData:
    """基金数据类"""

    def __init__(self):
        self.name: str = ""
        self.code: str = ""
        self.net_worth_trend: List[Dict] = []
        self.syl_1y: str = ""  # 近一月收益率
        self.syl_3y: str = ""  # 近三月收益率
        self.syl_6y: str = ""  # 近六月收益率
        self.syl_1n: str = ""  # 近一年收益率
        self.current_net_worth: float = 0.0  # 当前净值
        self.return_data: List[Dict] = []  # 收益率走势数据


def _parse_fund_data(content: str, fund_code: str) -> Optional[FundData]:
    """从获取的js脚本内容中解析基金数据"""
    fund_data = FundData()
    fund_data.code = fund_code

    # 解析基金名称
    name_match = re.search(r'var fS_name = "([^"]+)";', content)
    if name_match:
        fund_data.name = name_match.group(1)
    else:
        # 如果没有名称，后续解析无意义
        logger.warning(f"解析基金 {fund_code} 名称失败")
        return None

    # 解析收益率数据
    patterns = {
        "syl_1y": r'var syl_1y="([^"]*)";',
        "syl_3y": r'var syl_3y="([^"]*)";',
        "syl_6y": r'var syl_6y="([^"]*)";',
        "syl_1n": r'var syl_1n="([^"]*)";',
    }
    for attr, pattern in patterns.items():
        match = re.search(pattern, content)
        if match and match.group(1):
            setattr(fund_data, attr, match.group(1))

    # 解析收益率走势数据
    return_pattern = r"var Data_grandTotal = (\[.*?\]);"
    return_match = re.search(return_pattern, content, re.DOTALL)
    if return_match:
        try:
            import json

            return_data_str = return_match.group(1)
            fund_data.return_data = json.loads(return_data_str) if return_data_str else []
        except json.JSONDecodeError:
            logger.warning(f"解析基金 {fund_code} 收益率数据失败")

    return fund_data


async def fetch_fund_data(fund_code: str) -> Optional[FundData]:
    """
    获取基金数据

    Args:
        fund_code: 基金代码

    Returns:
        基金数据对象，如果获取失败返回None
    """
    url = f"http://fund.eastmoney.com/pingzhongdata/{fund_code}.js"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url)
            response.raise_for_status()
            content = response.text

        fund_data = _parse_fund_data(content, fund_code)
        if fund_data:
            logger.info(f"成功获取基金 {fund_code} 的数据")
            return fund_data

        logger.warning(f"解析基金 {fund_code} 数据失败")
        return None

    except httpx.TimeoutException:
        logger.error(f"获取基金 {fund_code} 数据超时")
        return None
    except httpx.HTTPStatusError as e:
        logger.error(f"获取基金 {fund_code} 数据失败，状态码: {e.response.status_code}")
        return None
    except Exception as e:
        logger.error(f"获取基金 {fund_code} 数据时发生错误: {e}")
        return None


def format_fund_message(fund_data: FundData) -> str:
    """格式化基金信息消息

    Args:
        fund_data: 基金数据

    Returns:
        格式化的消息字符串
    """
    message_parts = []

    # 基金名称和代码
    message_parts.append(f"📈 {fund_data.name}")
    message_parts.append(f"代码: {fund_data.code}")

    # 当前净值
    if fund_data.current_net_worth:
        message_parts.append(f"当前净值: {fund_data.current_net_worth:.4f}")

    # 收益率信息
    if fund_data.syl_1y:
        message_parts.append(f"近1月: {fund_data.syl_1y}%")
    if fund_data.syl_3y:
        message_parts.append(f"近3月: {fund_data.syl_3y}%")
    if fund_data.syl_6y:
        message_parts.append(f"近6月: {fund_data.syl_6y}%")
    if fund_data.syl_1n:
        message_parts.append(f"近1年: {fund_data.syl_1n}%")

    return "\n".join(message_parts)


def generate_return_chart(fund_data: FundData) -> bytes:
    """
    生成基金收益率走势图

    Args:
        fund_data: 基金数据字典，包含收益率历史数据

    Returns:
        bytes: PNG格式的图表数据
    """
    import pathlib

    import matplotlib.font_manager as fm

    font_path = str(
        pathlib.Path(__file__).resolve().parent.parent.parent.parent
        / "fonts"
        / "SourceHanSansSC-Regular.ttf"
    )
    font_prop = fm.FontProperties(fname=font_path)
    plt.rcParams["axes.unicode_minus"] = False

    plt.style.use("bmh")
    fig, ax = plt.subplots(figsize=(12, 6))

    return_data = fund_data.return_data

    if not return_data:
        ax.text(
            0.5,
            0.5,
            "暂无收益率数据",
            ha="center",
            va="center",
            transform=ax.transAxes,
            fontsize=16,
            fontproperties=font_prop,
        )
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
    else:
        # 过滤最近12个月的数据
        # 虽然好像给的数据最多只有6个月，以防万一
        twelve_months_ago = datetime.now() - timedelta(days=365)

        # 绘制每个系列的数据
        colors = ["#1f77b4", "#ff7f0e", "#2ca02c"]
        legend_handles = []
        for i, series in enumerate(return_data):
            name = series.get("name", f"系列{i + 1}")
            data_points = series.get("data", [])

            if data_points:
                recent_data = []
                for point in data_points:
                    timestamp = point[0] / 1000  # 转换为秒
                    date = datetime.fromtimestamp(timestamp)
                    if date >= twelve_months_ago:
                        recent_data.append((date, point[1]))

                if recent_data:
                    dates, values = zip(*recent_data, strict=True)
                    (line,) = ax.plot(
                        dates,
                        values,
                        linewidth=2,
                        color=colors[i % len(colors)],
                        label=name,
                        alpha=0.8,
                    )
                    legend_handles.append(line)

        # 设置图表标题和标签
        fund_name = fund_data.name or "基金"
        fund_code = fund_data.code or ""
        ax.set_title(
            f"{fund_name}({fund_code})", fontsize=14, fontweight="bold", fontproperties=font_prop
        )
        ax.set_xlabel("日期", fontsize=12, fontproperties=font_prop)
        ax.set_ylabel("收益率 (%)", fontsize=12, fontproperties=font_prop)

        # 格式化x轴日期显示
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=1))

        # 设置网格和图例
        ax.grid(True, alpha=0.3)
        ax.legend(handles=legend_handles, loc="upper left", fontsize=10, prop=font_prop)

        # 设置日期标签为水平
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=0, ha="center")

        # 添加零线
        ax.axhline(y=0, color="black", linestyle="-", alpha=0.3, linewidth=0.8)

    # 调整布局
    plt.tight_layout()

    img_buffer = io.BytesIO()
    plt.savefig(img_buffer, format="png", dpi=300, bbox_inches="tight")
    img_buffer.seek(0)
    img_data = img_buffer.getvalue()
    plt.close(fig)

    return img_data


@fund_query.handle()
async def handle_fund_query(bot: Bot, event: Event):
    """
    处理基金查询请求

    Args:
        bot: Bot实例
        event: 事件对象
    """
    fund_code = str(event.get_message()).strip()

    if not re.match(r"^\d{6}$", fund_code):
        return

    fund_data = await fetch_fund_data(fund_code)

    if fund_data:
        try:
            message = format_fund_message(fund_data)
            chart_data = generate_return_chart(fund_data)
            combined_message = message + MessageSegment.image(chart_data)
            await bot.send(event, combined_message)
        except Exception as e:
            logger.error(f"发送基金信息失败: {e}")
            # 如果图表生成失败，至少发送文本信息
            message = format_fund_message(fund_data)
            await fund_query.finish(message)
