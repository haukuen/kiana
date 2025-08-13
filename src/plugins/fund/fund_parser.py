import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class FundInfo:
    """用于存储解析后的基金信息"""
    name: str | None = None
    code: str | None = None
    source_rate: str | None = None
    rate: str | None = None
    min_purchase: str | None = None
    stock_codes: list[str] | None = None
    bond_codes: str | None = None
    stock_codes_new: list[str] | None = None
    bond_codes_new: str = None
    syl_1n: str | None = None # 近一月收益率
    syl_6y: str | None = None # 近三月收益率
    syl_3y: str | None = None # 近六月收益率
    syl_1y: str | None = None # 近一年收益率
    fund_shares_positions: list[tuple[int, float]] | None = None
    net_worth_trend: list[dict[str, Any]] | None = None
    return_data: list[dict[str, Any]] | None = None  # 收益率走势数据

def parse_fund_js(js_content: str) -> FundInfo:
    """
    解析基金数据的JS文件内容
    """
    def get_val(var_name):
        pattern = re.compile(fr'var {var_name}\s*=\s*(.*?);', re.DOTALL)
        match = pattern.search(js_content)
        if not match:
            return None

        value_str = match.group(1)
        print(value_str)
        return json.loads(value_str)

    # 返回一个包含所有解析出数据的FundInfo对象
    return FundInfo(
        name=get_val('fS_name'),
        code=get_val('fS_code'),
        source_rate=get_val('fund_sourceRate'),
        rate=get_val('fund_Rate'),
        min_purchase=get_val('fund_minsg'),
        stock_codes=get_val('stockCodes'),
        bond_codes=get_val('zqCodes'),
        stock_codes_new=get_val('stockCodesNew'),
        bond_codes_new=get_val('zqCodesNew'),
        syl_1n=get_val('syl_1n'),
        syl_6y=get_val('syl_6y'),
        syl_3y=get_val('syl_3y'),
        syl_1y=get_val('syl_1y'),
        fund_shares_positions=get_val('Data_fundSharesPositions'),
        net_worth_trend=get_val('Data_netWorthTrend'),
        return_data=get_val('Data_grandTotal')  # 收益率走势数据
    )

