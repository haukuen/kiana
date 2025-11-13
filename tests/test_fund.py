import pytest


@pytest.mark.asyncio
async def test_fund_query_002170_output_consistency(fund_plugin):
    """测试代码 002170 的输出一致性"""
    assert fund_plugin is not None, "fund 插件应该正确加载"
    assert fund_plugin.name == "fund", "插件名称应该是 fund"

    from src.plugins.fund import fund_query, identify_code_type, CodeType

    assert fund_query is not None, "fund_query 匹配器应该存在"
    assert callable(identify_code_type), "identify_code_type 应该是可调用的函数"

    code_type = identify_code_type("002170")
    assert code_type == CodeType.OFF_MARKET_FUND, (
        f"002170 应该被识别为场外基金，实际识别为: {code_type}"
    )

    if hasattr(fund_plugin, "metadata") and fund_plugin.metadata:
        assert "基金查询插件" in fund_plugin.metadata.description

    assert hasattr(fund_query, "type"), "匹配器应该有type属性"
    assert fund_query.type == "message", "匹配器类型应该是message"

    test_codes = ["002170", "510300", "000001.SH"]
    expected_types = [CodeType.OFF_MARKET_FUND, CodeType.ETF, CodeType.INDEX]

    for code, expected_type in zip(test_codes, expected_types):
        actual_type = identify_code_type(code)
        assert actual_type == expected_type, (
            f"代码 {code} 应该被识别为 {expected_type.value}，实际识别为: {actual_type.value}"
        )


@pytest.mark.asyncio
async def test_fund_query_002170_with_mocked_data():
    """测试代码 002170 的格式化输出一致性"""
    from src.plugins.fund import identify_code_type, CodeType, format_fund_info

    code_type = identify_code_type("002170")
    assert code_type == CodeType.OFF_MARKET_FUND, (
        f"002170 应该被识别为场外基金，实际识别为: {code_type}"
    )

    import pandas as pd

    basic_info_df = pd.DataFrame(
        {
            "item": ["基金名称", "基金代码", "基金类型"],
            "value": ["东吴移动互联混合C", "002170", "混合型"],
        }
    )

    # 业绩数据
    achievement_df = pd.DataFrame(
        {"周期": ["近1月", "近3月", "近6月", "近1年"], "本产品区间收益": [5.2, -2.1, 8.7, 15.3]}
    )

    # 净值数据
    nav_df = pd.DataFrame(
        {"净值日期": ["2024-01-01", "2024-01-02", "2024-01-03"], "日增长率": [0.5, -0.3, 1.2]}
    )

    mock_fund_data = {
        "basic_info": basic_info_df,
        "achievement": achievement_df,
        "nav": nav_df,
        "success": True,
    }

    formatted_output = await format_fund_info("002170", mock_fund_data)

    expected_baseline_output = """东吴移动互联混合C
代码: 002170

最近交易日收益:
2024-01-03: +1.20%
2024-01-02: -0.30%
2024-01-01: +0.50%

阶段收益:
近1月: 5.20%
近3月: -2.10%
近6月: 8.70%
近1年: 15.30%"""

    assert formatted_output == expected_baseline_output, (
        f"输出与基准不一致:\n实际输出:\n{formatted_output}\n期望基准:\n{expected_baseline_output}"
    )

    assert "东吴移动互联混合C" in formatted_output, "输出应该包含基金名称"
    assert "002170" in formatted_output, "输出应该包含基金代码"
    assert "最近交易日收益:" in formatted_output, "输出应该包含收益标题"
    assert "阶段收益:" in formatted_output, "输出应该包含阶段收益标题"

    lines = formatted_output.split("\n")
    assert lines[0] == "东吴移动互联混合C", f"第一行应该是基金名称，实际是: {lines[0]}"
    assert lines[1] == "代码: 002170", f"第二行应该是基金代码，实际是: {lines[1]}"


@pytest.mark.asyncio
async def test_fund_code_identification_consistency():
    """基金代码类型识别测试"""
    from src.plugins.fund import identify_code_type, CodeType

    test_cases = [
        ("002170", CodeType.OFF_MARKET_FUND),  # 东吴移动互联混合C
        ("018957", CodeType.OFF_MARKET_FUND),  # 中航机遇领航混合发起C
        ("510300", CodeType.ETF),  # 沪深300ETF
        ("159915", CodeType.ETF),  # 创业板ETF
        ("163406", CodeType.LOF),  # 兴全合润混合LOF
        ("000001.SZ", CodeType.STOCK),  # 平安银行
        ("600000.SH", CodeType.STOCK),  # 浦发银行
        ("000001.SH", CodeType.INDEX),  # 上证指数
        ("399001.SZ", CodeType.INDEX),  # 深证成指
    ]

    for code, expected_type in test_cases:
        code_type = identify_code_type(code)
        assert code_type == expected_type, (
            f"代码 {code} 应该被识别为 {expected_type.value}，实际识别为: {code_type.value}"
        )


@pytest.mark.asyncio
async def test_fund_format_structure_consistency():
    from src.plugins.fund import format_fund_info
    import pandas as pd

    basic_info_df = pd.DataFrame(
        {"item": ["基金名称", "基金代码"], "value": ["测试基金", "TEST001"]}
    )

    achievement_df = pd.DataFrame({"周期": ["近1月", "近3月"], "本产品区间收益": [1.0, 2.0]})

    nav_df = pd.DataFrame({"净值日期": ["2024-01-01", "2024-01-02"], "日增长率": [0.1, 0.2]})

    mock_fund_data = {
        "basic_info": basic_info_df,
        "achievement": achievement_df,
        "nav": nav_df,
        "success": True,
    }

    formatted_output = await format_fund_info("TEST001", mock_fund_data)

    lines = formatted_output.split("\n")

    # 验证基本结构
    assert len(lines) >= 8, "输出应该至少有8行"
    assert lines[0] == "测试基金", "第一行应该是基金名称"
    assert lines[1] == "代码: TEST001", "第二行应该是基金代码"
    assert "" == lines[2], "第三行应该是空行"
    assert "最近交易日收益:" == lines[3], "第四行应该是收益标题"

    assert "最近交易日收益:" in formatted_output, "应该包含最近交易日收益部分"
    assert "阶段收益:" in formatted_output, "应该包含阶段收益部分"
    assert "2024-01-01" in formatted_output, "应该包含第一个净值日期"
    assert "2024-01-02" in formatted_output, "应该包含第二个净值日期"
    assert "近1月" in formatted_output, "应该包含近1月数据"
    assert "近3月" in formatted_output, "应该包含近3月数据"


@pytest.mark.asyncio
async def test_fund_code_type_edge_cases():
    """基金代码边界情况测试"""
    from src.plugins.fund import identify_code_type, CodeType

    edge_cases = [
        ("", CodeType.UNKNOWN),
        ("ABC", CodeType.UNKNOWN),
        ("12345", CodeType.UNKNOWN),  # 5位数字
        ("1234567", CodeType.UNKNOWN),  # 7位数字
        ("123456789", CodeType.UNKNOWN),  # 9位数字
        # 有效代码格式
        ("002170", CodeType.OFF_MARKET_FUND),  # 6位数字 - 场外基金
        ("000001.SZ", CodeType.STOCK),  # 带交易所后缀
        ("600000.SH", CodeType.STOCK),  # 带交易所后缀
        ("000001.SH", CodeType.INDEX),  # 指数
        ("399001.SZ", CodeType.INDEX),  # 指数
    ]

    for code, expected_type in edge_cases:
        code_type = identify_code_type(code)
        assert code_type == expected_type, (
            f"代码 '{code}' 应该被识别为 {expected_type.value}，实际识别为: {code_type.value}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
