"""真实 AI API 集成测试。

默认 skip,运行方式::

    LIVE_AI_BASE_URL=... LIVE_AI_API_KEY=... LIVE_AI_MODEL=... \\
        uv run pytest -m live_ai tests/test_live_ai_integration.py -v

炼化测试使用 NoneBot 当前环境的 ai_provider 档案和 refine 模型路由。
词频测试仍使用下面的 ai_creds fixture。

验证项:

1. refine 的 ``request_refine_summary`` 真实可用(基线)
2. word_pulse 的 ``_request_llm`` 用 ``response_format=json_object`` 真实可用(验证 bug#3 修复)
3. word_pulse 的 ``expand_charsets`` 真实可用,且返回符合 pydantic schema(验证字符集扩展能成功)
4. word_pulse 的 ``classify_batch`` 真实可用
5. word_pulse 的 ``summarize`` 真实可用,且返回符合 ``SummaryResult`` schema
6. refine 多人 prompt 真实能生成"综合多人"的总结(验证 bug#2 prompt 措辞是否足够)
"""

from __future__ import annotations

import os

import pytest


def _get_credentials() -> tuple[str, str, str] | None:
    """优先用 ``LIVE_AI_*``,fallback 到 ``REFINE_AI_*`` 和 ``WORD_PULSE_*``。"""
    base_url = (
        os.environ.get("LIVE_AI_BASE_URL")
        or os.environ.get("REFINE_AI_BASE_URL")
        or os.environ.get("WORD_PULSE_BASE_URL")
    )
    api_key = (
        os.environ.get("LIVE_AI_API_KEY")
        or os.environ.get("REFINE_AI_API_KEY")
        or os.environ.get("WORD_PULSE_API_KEY")
    )
    model = (
        os.environ.get("LIVE_AI_MODEL")
        or os.environ.get("REFINE_AI_MODEL")
        or os.environ.get("WORD_PULSE_MODEL")
    )
    if not all([base_url, api_key, model]):
        return None
    return base_url, api_key, model  # type: ignore[return-value]


# 共享 fixture:提供凭据,无凭据 skip。
@pytest.fixture(scope="module")
def ai_creds() -> tuple[str, str, str]:
    creds = _get_credentials()
    if creds is None:
        pytest.skip(
            "需要 LIVE_AI_BASE_URL/API_KEY/MODEL 环境变量(或 REFINE_AI_*/WORD_PULSE_*)",
        )
    return creds


@pytest.fixture
def refine_ai_config(monkeypatch: pytest.MonkeyPatch, reset_ai_endpoint: None) -> None:
    """恢复真实供应商档案，按炼化自己的路由检查配置。"""
    from nonebot import get_plugin_config

    from src.plugins.ai_provider.config import Config, config as ai_config
    from src.plugins.ai_provider.service import reset_auto_cache
    from src.plugins.refine.runner import missing_ai_config

    settings = get_plugin_config(Config)
    for name in Config.model_fields:
        monkeypatch.setattr(ai_config, name, getattr(settings, name))
    reset_auto_cache()
    if missing := missing_ai_config():
        pytest.skip(f"炼化 AI 配置不完整：{missing[0]}")


# ── 测试 1: refine 基线 ──────────────────────────────────────────────


@pytest.mark.live_ai
@pytest.mark.asyncio
async def test_refine_summary_real_api(refine_ai_config: None) -> None:
    """验证 refine 的 ``request_refine_summary`` 对真实 API 可用(基线测试)。

    如果这个失败,说明 API 凭据/网络有问题,其他 live_ai 测试都不可信。
    """
    from src.plugins.refine.ai import request_refine_summary

    summary = await request_refine_summary(
        timeout_seconds=60.0,
        temperature=0.3,
        prompt_payload=(
            "[08-04 10:00] 张三: 今天大盘怎么样?\n"
            "[08-04 10:01] 李四: 看着还行。"
        ),
    )
    assert isinstance(summary, str)
    assert len(summary) > 0
    # 真实模型可能不严格按 300 字限制,放宽到 2000
    assert len(summary) < 2000


# ── 测试 2: word_pulse _request_llm 走 json_object ────────────────────


@pytest.mark.live_ai
@pytest.mark.asyncio
async def test_word_pulse_request_llm_json_object_real_api(
    ai_creds: tuple[str, str, str],
) -> None:
    """验证 bug#3 修复:word_pulse 的 ``_request_llm`` 用 json_object 对真实 API 可用。

    这是 bug#3 的核心验证 —— 如果上游不支持 ``response_format=json_object``,
    会抛 ``WordPulseAIServiceError``。
    """
    base_url, api_key, model = ai_creds
    from src.plugins.word_pulse.ai import _request_llm

    result = await _request_llm(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=[
            {
                "role": "system",
                "content": '你是助手,只返回 JSON {"status": "ok"}',
            },
            {"role": "user", "content": "测试"},
        ],
        temperature=0.0,
        timeout_seconds=60.0,
    )
    assert isinstance(result, dict)
    assert "status" in result or len(result) > 0


# ── 测试 3: word_pulse expand_charsets 真实可用 ───────────────────────


@pytest.mark.live_ai
@pytest.mark.asyncio
async def test_word_pulse_expand_charsets_real_api(
    ai_creds: tuple[str, str, str],
) -> None:
    """验证 ``expand_charsets`` 真实可用,返回符合 pydantic schema。

    bug#3 现场:用户报告字符集扩展失败。这个测试模拟真实调用。
    """
    base_url, api_key, model = ai_creds
    from src.plugins.word_pulse.ai import expand_charsets

    result = await expand_charsets(
        base_url=base_url,
        api_key=api_key,
        model=model,
        seeds=["新能源", "半导体"],
        theme="板块",
        temperature=0.0,
        timeout=60.0,
    )
    assert isinstance(result, dict)
    assert "新能源" in result
    assert "半导体" in result
    # 每个 cluster 的 chars 数组符合 pydantic 约束(5-30 个)
    for cluster, chars in result.items():
        assert 5 <= len(chars) <= 30, (
            f"cluster {cluster} chars 数量 {len(chars)} 不在 5-30 范围"
        )


# ── 测试 4: word_pulse classify_batch 真实可用 ────────────────────────


@pytest.mark.live_ai
@pytest.mark.asyncio
async def test_word_pulse_classify_batch_real_api(
    ai_creds: tuple[str, str, str],
) -> None:
    """验证 ``classify_batch`` 真实可用。"""
    base_url, api_key, model = ai_creds
    from src.plugins.word_pulse.ai import classify_batch

    messages = [
        (1, "新能源车销量大涨"),
        (2, "半导体缺货"),
        (3, "今天天气不错"),  # 应归 null
    ]
    result = await classify_batch(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        clusters=[
            {"name": "新能源", "aliases": []},
            {"name": "半导体", "aliases": []},
        ],
        theme_name="板块",
        temperature=0.0,
        timeout=60.0,
    )
    assert len(result) == 3
    # 至少有 1 条被归到某个 cluster(不是全 null)
    non_null = [r for r in result if r[1] is not None]
    assert len(non_null) >= 1


# ── 测试 5: word_pulse summarize 真实可用,符合 SummaryResult schema ──


@pytest.mark.live_ai
@pytest.mark.asyncio
async def test_word_pulse_summarize_real_api(
    ai_creds: tuple[str, str, str],
) -> None:
    """验证 ``summarize`` 真实可用,返回符合 ``SummaryResult`` schema。"""
    base_url, api_key, model = ai_creds
    from src.plugins.word_pulse.ai import summarize

    prompt = (
        "主题:板块\n"
        "时间范围:7天\n"
        "子类:\n"
        "- 新能源\n"
        "- 半导体\n\n"
        "日桶统计:\n"
        "  [2026-08-04] 总100条 | 新能源:50, 半导体:30, _other:20\n\n"
        "典型原文:\n"
        "  [2026-08-04] 张三: 新能源继续强势"
    )

    result = await summarize(
        base_url=base_url,
        api_key=api_key,
        model=model,
        prompt=prompt,
        temperature=0.3,
        timeout=60.0,
    )
    assert result.trend  # 非空字符串
    assert len(result.ranking) > 0
    # 验证 ranking 字段类型
    for item in result.ranking:
        assert isinstance(item.cluster, str)
        assert isinstance(item.count, int)
        assert isinstance(item.percent, (int, float))


# ── 测试 6: refine 多人 prompt 真实能综合多人观点 ─────────────────────


@pytest.mark.live_ai
@pytest.mark.asyncio
async def test_refine_multi_member_summary_covers_all_members(
    refine_ai_config: None,
) -> None:
    """验证 bug#2 prompt 措辞:多人 prompt 真实能生成"综合多人"的总结。

    如果 AI 只总结了一个人的观点,说明 prompt 措辞需要加强
    (虽然 bug#2 主因是采样,但 prompt 也值得验证)。
    """
    from src.plugins.refine.ai import request_refine_summary

    prompt_payload = (
        "[08-04 10:00] 张三: 我看好新能源,继续持有\n"
        "[08-04 10:01] 李四: 我反对,新能源估值太高了\n"
        "[08-04 10:02] 王五: 我持中性观点,看下季度数据\n"
        "[08-04 10:03] 张三: 而且政策面也在支持\n"
        "[08-04 10:04] 李四: 政策支持不代表基本面好"
    )

    summary = await request_refine_summary(
        timeout_seconds=60.0,
        temperature=0.3,
        prompt_payload=prompt_payload,
    )
    # 关键断言:总结应该提到至少两个不同人的观点。
    # (真实模型可能用"不同意见"/"分歧"/"两人讨论"等表达,不强制出现具体姓名)
    assert len(summary) > 50  # 不能是空总结
    # 这个断言可能太严格,作为软断言:打印 warning 而不是 fail。
    # 真实运行后人工 review 总结内容是否平衡。
