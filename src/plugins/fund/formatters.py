"""基金数据格式化模块"""

from typing import Any

import pandas as pd
from nonebot import logger

from .data_fetcher import get_stock_name
from .runtime import get_plugin_config_cached


def _get_config_value(attr_name: str, default_value: Any) -> Any:
    """安全地获取配置值，如果 NoneBot 未初始化则使用默认值"""
    try:
        config = get_plugin_config_cached()
        return getattr(config, attr_name, default_value)
    except ValueError:
        # NoneBot has not been initialized
        return default_value


# 默认配置常量
DEFAULT_DISPLAY_RECENT_DAYS = 7


def _extract_price_data(latest: pd.Series, previous: pd.Series | None) -> dict | None:
    """提取并计算价格数据

    Args:
        latest: 最新交易日数据
        previous: 前一交易日数据

    Returns:
        包含价格信息的字典，如果数据异常则返回 None
    """
    try:
        latest_price = float(latest.get("close") or 0)
        open_price = float(latest.get("open") or 0)
        high = float(latest.get("high") or 0)
        low = float(latest.get("low") or 0)

        # 计算涨跌幅和涨跌额
        if previous is not None:
            previous_close = float(previous.get("close") or 0)
            change_amount = latest_price - previous_close
            change_pct = (change_amount / previous_close * 100) if previous_close != 0 else 0
        else:
            change_amount = 0
            change_pct = 0

        return {
            "latest_price": latest_price,
            "open_price": open_price,
            "high": high,
            "low": low,
            "change_amount": change_amount,
            "change_pct": change_pct,
        }
    except (ValueError, TypeError) as e:
        logger.error(f"数据转换失败: {e}")
        return None


def _build_index_info_header(
    index_code: str, index_data: dict, price_data: dict, latest: pd.Series
) -> list[str]:
    """构建指数信息头部

    Args:
        index_code: 指数代码
        index_data: 指数数据字典
        price_data: 价格数据字典
        latest: 最新交易日数据

    Returns:
        信息行列表
    """
    # 指数名称映射
    index_names = {
        "sh000001": "上证指数",
        "sz399001": "深证成指",
        "sh000300": "沪深300",
        "sz399006": "创业板指",
        "sz399005": "中小板指",
        "sh000016": "上证50",
        "sz399102": "创业板综",
    }
    symbol = index_data.get("symbol", "")
    index_name = index_names.get(symbol, f"指数 {index_code}")

    volume = latest.get("volume", "N/A")
    amount = latest.get("amount", "N/A")
    date_str = latest.get("date", "")

    # 构建信息文本
    info_lines = [
        index_name,
        f"代码: {index_code}",
        "",
        f"最新点位: {price_data['latest_price']:.2f}",
    ]

    # 添加涨跌幅信息
    change_pct = price_data["change_pct"]
    change_amount = price_data["change_amount"]
    if change_pct > 0:
        info_lines.append(f"涨跌幅: +{change_pct:.2f}% (+{change_amount:.2f})")
    else:
        info_lines.append(f"涨跌幅: {change_pct:.2f}% ({change_amount:.2f})")

    info_lines.extend(
        [
            f"今开: {price_data['open_price']:.2f}  最高: {price_data['high']:.2f}  最低: {price_data['low']:.2f}",
            f"成交量: {volume}",
            f"成交额: {amount}",
            f"日期: {date_str}",
            "",
            "最近交易日涨跌:",
        ]
    )

    return info_lines


def _add_recent_changes(info_lines: list[str], hist_df: pd.DataFrame) -> None:
    """添加最近交易日涨跌幅信息

    Args:
        info_lines: 信息行列表（就地修改）
        hist_df: 历史数据
    """
    display_days = _get_config_value("fund_display_recent_days", DEFAULT_DISPLAY_RECENT_DAYS)
    recent_hist = hist_df.tail(display_days).iloc[::-1]

    for i, (_, row) in enumerate(recent_hist.iterrows()):
        try:
            date_str = row.get("date", "")
            close_price = float(row.get("close", 0))

            # 计算当日涨跌幅
            if i < len(recent_hist) - 1:
                prev_close = float(recent_hist.iloc[i + 1].get("close", 0))
                daily_change = (
                    (close_price - prev_close) / prev_close * 100 if prev_close != 0 else 0
                )
            else:
                daily_change = 0

            change_sign = "+" if daily_change > 0 else ""
            info_lines.append(f"{date_str}: {change_sign}{daily_change:.2f}% ({close_price:.2f})")
        except (ValueError, TypeError):
            continue


def format_index_info(index_code: str, index_data: dict) -> str:
    """格式化指数信息文本

    Args:
        index_code: 指数代码
        index_data: 指数数据字典

    Returns:
        格式化后的信息文本
    """
    try:
        hist_df = index_data["hist_data"]

        if hist_df.empty or len(hist_df) == 0:
            return f"指数 {index_code}\n暂无数据"

        # 获取最新交易日数据
        latest = hist_df.iloc[-1]
        previous = hist_df.iloc[-2] if len(hist_df) > 1 else None

        # 提取价格数据
        price_data = _extract_price_data(latest, previous)
        if price_data is None:
            return f"指数 {index_code}\n数据格式异常"

        # 构建信息头部
        info_lines = _build_index_info_header(index_code, index_data, price_data, latest)

        # 添加最近交易日涨跌幅
        _add_recent_changes(info_lines, hist_df)

        return "\n".join(info_lines)

    except Exception as e:
        logger.error(f"格式化指数信息失败 [{index_code}]: {e}", exc_info=True)
        return f"指数 {index_code}\n数据格式化失败: {e!s}"


async def format_stock_info(stock_code: str, stock_data: dict) -> str:
    """格式化股票信息文本

    Args:
        stock_code: 股票代码
        stock_data: 股票数据字典

    Returns:
        格式化后的信息文本
    """
    try:
        hist_df = stock_data["hist_data"]

        if hist_df.empty or len(hist_df) == 0:
            return f"股票 {stock_code}\n暂无数据"

        # 获取最新交易日数据
        latest = hist_df.iloc[-1]

        # 获取股票名称
        stock_name = await get_stock_name(stock_code)

        # 安全地获取数值数据
        try:
            latest_price = float(latest.get("收盘", 0))
            change_pct = float(latest.get("涨跌幅", 0))
            change_amount = float(latest.get("涨跌额", 0))
            high = float(latest.get("最高", 0))
            low = float(latest.get("最低", 0))
            open_price = float(latest.get("开盘", 0))
        except (ValueError, TypeError) as e:
            logger.error(f"股票 {stock_code} 数据转换失败: {e}")
            return f"股票 {stock_code}\n数据格式异常"

        volume = latest.get("成交量", "N/A")
        turnover = latest.get("成交额", "N/A")

        # 构建信息文本
        info_lines = [
            stock_name,
            f"代码: {stock_code}",
            "",
            f"最新价: {latest_price:.2f}",
        ]

        if change_pct > 0:
            info_lines.append(f"涨跌幅: +{change_pct:.2f}% (+{change_amount:.2f})")
        else:
            info_lines.append(f"涨跌幅: {change_pct:.2f}% ({change_amount:.2f})")

        info_lines.extend(
            [
                f"今开: {open_price:.2f}  最高: {high:.2f}  最低: {low:.2f}",
                f"成交量: {volume}",
                f"成交额: {turnover}",
                "",
                "最近交易日涨跌:",
            ]
        )

        # 添加最近交易日的涨跌幅
        display_days = _get_config_value("fund_display_recent_days", DEFAULT_DISPLAY_RECENT_DAYS)
        recent_hist = hist_df.tail(display_days).iloc[::-1]
        for _, row in recent_hist.iterrows():
            try:
                date_str = row.get("日期", "")
                daily_change = float(row.get("涨跌幅", 0))
                close_price = float(row.get("收盘", 0))

                if daily_change > 0:
                    info_lines.append(f"{date_str}: +{daily_change:.2f}% ({close_price:.2f})")
                else:
                    info_lines.append(f"{date_str}: {daily_change:.2f}% ({close_price:.2f})")
            except (ValueError, TypeError):
                continue

        return "\n".join(info_lines)

    except Exception as e:
        logger.error(f"格式化股票信息失败 [{stock_code}]: {e}", exc_info=True)
        return f"股票 {stock_code}\n数据格式化失败: {e!s}"


def format_etf_info(fund_code: str, etf_data: dict) -> str:
    """格式化场内ETF/LOF基金信息文本

    Args:
        fund_code: 基金代码
        etf_data: ETF数据字典

    Returns:
        格式化后的信息文本
    """
    try:
        spot_info = etf_data["spot_info"]
        hist_df = etf_data["hist_data"]

        # 安全地获取数据
        fund_name = spot_info.get("名称", f"基金 {fund_code}")

        try:
            latest_price = float(spot_info.get("最新价", 0))
            change_pct = float(spot_info.get("涨跌幅", 0))
            change_amount = float(spot_info.get("涨跌额", 0))
        except (ValueError, TypeError) as e:
            logger.error(f"ETF {fund_code} 数据转换失败: {e}")
            return f"基金 {fund_code}\n数据格式异常"

        volume = spot_info.get("成交量", "N/A")
        turnover = spot_info.get("成交额", "N/A")

        # 构建信息文本
        info_lines = [
            fund_name,
            f"代码: {fund_code}",
            "",
            f"最新价: {latest_price:.3f}",
        ]

        if change_pct > 0:
            info_lines.append(f"涨跌幅: +{change_pct:.2f}% (+{change_amount:.3f})")
        else:
            info_lines.append(f"涨跌幅: {change_pct:.2f}% ({change_amount:.3f})")

        info_lines.extend(
            [
                f"成交量: {volume}",
                f"成交额: {turnover}",
                "",
                "最近交易日涨跌:",
            ]
        )

        # 添加最近交易日的涨跌幅
        display_days = _get_config_value("fund_display_recent_days", DEFAULT_DISPLAY_RECENT_DAYS)
        recent_hist = hist_df.tail(display_days).iloc[::-1]
        for _, row in recent_hist.iterrows():
            try:
                date_str = row.get("日期", "")
                daily_change = float(row.get("涨跌幅", 0))
                close_price = float(row.get("收盘", 0))

                if daily_change > 0:
                    info_lines.append(f"{date_str}: +{daily_change:.2f}% ({close_price:.3f})")
                else:
                    info_lines.append(f"{date_str}: {daily_change:.2f}% ({close_price:.3f})")
            except (ValueError, TypeError):
                continue

        return "\n".join(info_lines)

    except Exception as e:
        logger.error(f"格式化ETF信息失败 [{fund_code}]: {e}", exc_info=True)
        return f"基金 {fund_code}\n数据格式化失败: {e!s}"


def format_fund_info(fund_code: str, fund_data: dict) -> str:
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

        # 获取最近交易日的数据
        display_days = _get_config_value("fund_display_recent_days", DEFAULT_DISPLAY_RECENT_DAYS)
        recent_nav = nav_df.tail(display_days).iloc[::-1]

        # 构建信息文本
        info_lines = [
            fund_name,
            f"代码: {fund_code}",
            "",
            "最近交易日收益:",
        ]

        for _, row in recent_nav.iterrows():
            try:
                date_str = row.get("净值日期", "")
                daily_return = float(row.get("日增长率", 0))
                if daily_return > 0:
                    info_lines.append(f"{date_str}: +{daily_return:.2f}%")
                else:
                    info_lines.append(f"{date_str}: {daily_return:.2f}%")
            except (ValueError, TypeError):
                continue

        info_lines.extend(["", "阶段收益:"])

        # 添加阶段收益数据
        stage_periods = ["近1月", "近3月", "近6月", "近1年", "近3年", "近5年"]
        for period in stage_periods:
            try:
                period_data = achievement_df[achievement_df["周期"] == period]
                if not period_data.empty:
                    return_rate = float(period_data.iloc[0]["本产品区间收益"])
                    info_lines.append(f"{period}: {return_rate:.2f}%")
            except (KeyError, ValueError, IndexError) as e:
                # 如果某个周期的数据不存在或格式错误,跳过该周期
                logger.debug(f"跳过周期 {period} 的数据: {e}")
                continue

        return "\n".join(info_lines)

    except Exception as e:
        logger.error(f"格式化基金信息失败 [{fund_code}]: {e}", exc_info=True)
        return f"基金 {fund_code}\n数据格式化失败: {e!s}"


def format_fund_holdings(fund_code: str, holdings_data: dict) -> str:
    """格式化基金十大重仓股信息"""
    try:
        holdings_df = holdings_data["holdings"]

        if holdings_df.empty:
            return f"基金 {fund_code}\n暂无持仓数据"

        # 获取最新季度的数据
        unique_quarters = holdings_df["季度"].unique()
        latest_quarter = max(unique_quarters)
        latest_holdings = holdings_df[holdings_df["季度"] == latest_quarter].head(10)

        info_lines = [
            f"十大重仓股 ({latest_quarter})",
            "",
        ]

        for idx, (_, row) in enumerate(latest_holdings.iterrows(), 1):
            try:
                stock_code = row.get("股票代码", "")
                stock_name = row.get("股票名称", "")
                ratio = float(row.get("占净值比例", 0))
                info_lines.append(f"{idx}. {stock_name}({stock_code}) {ratio:.2f}%")
            except (ValueError, TypeError):
                continue

        return "\n".join(info_lines)

    except Exception as e:
        logger.error(f"格式化基金持仓信息失败 [{fund_code}]: {e}", exc_info=True)
        return f"基金 {fund_code}\n持仓数据格式化失败: {e!s}"
