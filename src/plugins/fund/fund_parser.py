import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class FundInfo:
    """用于存储解析后的基金信息"""

    name: str | None = None
    code: str | None = None
    source_rate: str | None = None  # 原费率
    rate: str | None = None  # 现费率
    min_purchase: str | None = None  # 最小申购金额
    stock_codes: list[str] | None = None  # 基金持仓股票代码
    bond_codes: str | None = None  # 基金持仓债券代码
    stock_codes_new: list[str] | None = None  # 基金持仓股票代码（新市场号）
    bond_codes_new: str | None = None  # 基金持仓债券代码（新市场号）
    syl_1y: str | None = None  # 近一月收益率
    syl_3y: str | None = None  # 近三月收益率
    syl_6y: str | None = None  # 近六月收益率
    syl_1n: str | None = None  # 近一年收益率
    fund_shares_positions: list[tuple[int, float]] | None = None  # 股票仓位测算图
    net_worth_trend: list[dict[str, Any]] | None = None
    return_data: list[dict[str, Any]] | None = None  # 收益率走势数据


def parse_fund_js(js_content: str) -> FundInfo:
    """
    解析基金数据的JS文件内容
    """

    def get_val(var_name):
        pattern = re.compile(rf"var {var_name}\s*=\s*(.*?);", re.DOTALL)
        match = pattern.search(js_content)
        if not match:
            return None

        value_str = match.group(1)
        return json.loads(value_str)

    return FundInfo(
        name=get_val("fS_name"),
        code=get_val("fS_code"),
        source_rate=get_val("fund_sourceRate"),
        rate=get_val("fund_Rate"),
        min_purchase=get_val("fund_minsg"),
        stock_codes=get_val("stockCodes"),
        bond_codes=get_val("zqCodes"),
        stock_codes_new=get_val("stockCodesNew"),
        bond_codes_new=get_val("zqCodesNew"),
        syl_1n=get_val("syl_1n"),
        syl_6y=get_val("syl_6y"),
        syl_3y=get_val("syl_3y"),
        syl_1y=get_val("syl_1y"),
        fund_shares_positions=get_val("Data_fundSharesPositions"),
        net_worth_trend=get_val("Data_netWorthTrend"),
        return_data=get_val("Data_grandTotal"),
    )


def get_recent_daily_returns(
    net_worth_trend: list[dict[str, Any]], days: int = 3
) -> list[dict[str, Any]]:
    """
    从净值走势数据中提取最近几日的涨跌幅信息

    Args:
        net_worth_trend: 净值走势数据列表
        days: 要获取的天数，默认3天

    Returns:
        包含日期、涨跌幅和净值的字典列表，按日期倒序排列
    """
    if not net_worth_trend:
        return []

    recent_returns = []

    # 取最后几条数据
    recent_data = net_worth_trend[-days:] if len(net_worth_trend) >= days else net_worth_trend

    for item in reversed(recent_data):  # 倒序，最新的在前
        # 直接使用原数据的时间戳，留给调用方转换 1754928000000
        date_str = item.get("x", "未知日期")
        equity_return = item.get("equityReturn", 0)
        net_worth = item.get("y", 0)

        recent_returns.append(
            {"date": str(date_str), "equity_return": equity_return, "net_worth": net_worth}
        )

    return recent_returns
