import akshare as ak
from nonebot import logger, on_regex
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, MessageEvent, MessageSegment
from nonebot.exception import MatcherException
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="fund",
    description="基金查询插件",
    usage="发送基金代码查询基金信息，如：018957",
)

fund_query = on_regex(r"^\d{6}$")


async def get_fund_data(fund_code: str) -> dict:
    """获取基金数据，包括基本信息、业绩和净值信息"""
    try:
        # 获取基金基本信息
        basic_info_df = ak.fund_individual_basic_info_xq(symbol=fund_code)

        if basic_info_df.empty or len(basic_info_df) == 0:
            return {"success": False}

        # 获取基金业绩数据
        achievement_df = ak.fund_individual_achievement_xq(symbol=fund_code)

        # 获取基金净值数据
        nav_df = ak.fund_open_fund_info_em(symbol=fund_code, indicator="单位净值走势")

        # 检查净值数据是否有效
        if nav_df.empty or len(nav_df) == 0:
            return {"success": False}

        return {
            "basic_info": basic_info_df,
            "achievement": achievement_df,
            "nav": nav_df,
            "success": True,
        }
    except Exception:
        return {"success": False}


async def get_fund_holdings(fund_code: str) -> dict:
    """获取基金十大重仓股信息"""
    try:
        from datetime import datetime

        current_year = datetime.now().year

        # 获取基金持仓数据
        holdings_df = ak.fund_portfolio_hold_em(symbol=fund_code, date=str(current_year))

        return {
            "holdings": holdings_df,
            "success": True,
        }
    except Exception as e:
        logger.error(f"获取基金持仓数据失败: {e}")
        return {"success": False, "error": str(e)}


async def format_fund_info(fund_code: str, fund_data: dict) -> str:
    """格式化基金信息文本"""
    try:
        basic_info_df = fund_data["basic_info"]
        achievement_df = fund_data["achievement"]
        nav_df = fund_data["nav"]

        # 从基本信息中获取基金名称
        fund_name_row = basic_info_df[basic_info_df["item"] == "基金名称"]
        if not fund_name_row.empty:
            fund_name = fund_name_row.iloc[0]["value"]
        else:
            fund_name = f"基金 {fund_code}"

        # 获取最近7个交易日的数据
        recent_nav = nav_df.tail(7).iloc[::-1]

        # 构建信息文本
        info_lines = []
        info_lines.append(fund_name)
        info_lines.append(f"代码: {fund_code}")
        info_lines.append("")

        # 添加最近7个交易日的收益率
        info_lines.append("最近交易日收益:")
        for _, row in recent_nav.iterrows():
            date_str = row["净值日期"]
            daily_return = float(row["日增长率"])
            if daily_return > 0:
                info_lines.append(f"{date_str}: +{daily_return:.2f}%")
            else:
                info_lines.append(f"{date_str}: {daily_return:.2f}%")

        info_lines.append("")

        # 添加阶段收益数据
        info_lines.append("阶段收益:")
        stage_periods = ["近1月", "近3月", "近6月", "近1年", "近3年", "近5年"]

        for period in stage_periods:
            try:
                period_data = achievement_df[achievement_df["周期"] == period]
                if not period_data.empty:
                    return_rate = float(period_data.iloc[0]["本产品区间收益"])
                    info_lines.append(f"{period}: {return_rate:.2f}%")
            except (KeyError, ValueError, IndexError) as e:
                # 如果某个周期的数据不存在或格式错误，跳过该周期
                logger.debug(f"跳过周期 {period} 的数据: {e}")
                continue

        return "\n".join(info_lines)

    except Exception as e:
        logger.error(f"格式化基金信息失败: {e}")
        return f"基金 {fund_code}\n数据格式化失败: {e!s}"


async def format_fund_holdings(fund_code: str, holdings_data: dict) -> str:
    """格式化基金十大重仓股信息"""
    try:
        holdings_df = holdings_data["holdings"]

        if holdings_df.empty:
            return f"基金 {fund_code}\n暂无持仓数据"

        # 获取最新季度的数据
        # 找到所有不同的季度，并选择最新的一个
        unique_quarters = holdings_df["季度"].unique()
        # 按季度排序，取最新的（假设季度格式为"2025年X季度股票投资明细"）
        latest_quarter = sorted(unique_quarters, reverse=True)[0]
        latest_holdings = holdings_df[holdings_df["季度"] == latest_quarter].head(10)

        info_lines = []
        info_lines.append(f"十大重仓股 ({latest_quarter})")
        info_lines.append("")

        for idx, (_, row) in enumerate(latest_holdings.iterrows(), 1):
            stock_code = row["股票代码"]
            stock_name = row["股票名称"]
            ratio = float(row["占净值比例"])
            info_lines.append(f"{idx}. {stock_name}({stock_code}) {ratio:.2f}%")

        return "\n".join(info_lines)

    except Exception as e:
        logger.error(f"格式化基金持仓信息失败: {e}")
        return f"基金 {fund_code}\n持仓数据格式化失败: {e!s}"


async def create_forward_nodes(
    bot: Bot,
    info_text: str,
    holdings_text: str | None = None,
    media_segments: list[MessageSegment] | None = None,
) -> list[dict]:
    """创建合并转发消息节点"""
    forward_nodes = []

    # 第一个节点：基金基本信息
    text_node = {
        "type": "node",
        "data": {"name": "", "uin": bot.self_id, "content": info_text},
    }
    forward_nodes.append(text_node)

    # 第二个节点：十大重仓股信息
    if holdings_text:
        holdings_node = {
            "type": "node",
            "data": {"name": "", "uin": bot.self_id, "content": holdings_text},
        }
        forward_nodes.append(holdings_node)

    return forward_nodes


async def send_forward_message(bot: Bot, event: MessageEvent, forward_nodes: list):
    """发送合并转发消息"""
    if isinstance(event, GroupMessageEvent):
        await bot.call_api(
            "send_group_forward_msg",
            group_id=event.group_id,
            messages=forward_nodes,
        )
    else:
        await bot.call_api(
            "send_private_forward_msg",
            user_id=event.user_id,
            messages=forward_nodes,
        )


@fund_query.handle()
async def handle_fund_query(bot: Bot, event: MessageEvent):
    """处理基金查询请求"""
    fund_code = str(event.message).strip()

    try:
        # 获取基金数据
        fund_data = await get_fund_data(fund_code)

        if not fund_data["success"]:
            return

        # 格式化基金信息
        info_text = await format_fund_info(fund_code, fund_data)

        # 获取基金持仓数据
        holdings_data = await get_fund_holdings(fund_code)
        holdings_text = None

        if holdings_data["success"]:
            holdings_text = await format_fund_holdings(fund_code, holdings_data)
        else:
            logger.warning(f"获取基金持仓数据失败: {holdings_data.get('error', '未知错误')}")

        # 创建合并转发消息节点
        forward_nodes = await create_forward_nodes(bot, info_text, holdings_text)

        # 发送合并转发消息
        await send_forward_message(bot, event, forward_nodes)

    except MatcherException:
        raise
    except Exception:
        return
