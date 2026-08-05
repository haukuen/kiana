"""词频插件分析层（classify_message）+ 端到端 alias 行为测试。

classify_message 本身无需修改，它接受任意 cluster_terms。
alias 行为的端到端验证：cluster_terms 构造时把 alias 并入，
classify_message 应当对纯 alias 命中的消息返回主名词。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from nonebug import App


def test_classify_message_hits_alias_only(app: App) -> None:
    """消息只含 alias（不含主名词），应归类到主名词。"""
    from src.plugins.word_pulse.analysis import classify_message
    char_pool = {"茅", "子", "台", "飞"}
    # 模拟 _compute_buckets 里 alias 被并入 cluster_terms 后的形态
    cluster_terms = {
        "茅台": {"茅台", "茅子", "飞天"},  # 主名词 + aliases
        "五粮液": {"五粮液"},
    }
    # 消息只出现"茅子"，没出现"茅台"
    assert classify_message("今天茅子涨疯了", char_pool, cluster_terms) == ["茅台"]


def test_classify_message_no_hit_returns_none(app: App) -> None:
    """消息不含 char_pool 任何字符，应返回 None。"""
    from src.plugins.word_pulse.analysis import classify_message
    char_pool = {"茅"}
    cluster_terms = {"茅台": {"茅台"}}
    assert classify_message("中午吃啥", char_pool, cluster_terms) is None


def test_classify_message_char_hit_but_no_term_grey(app: App) -> None:
    """消息含 char_pool 字符但无 cluster term 命中，返回 GREY。"""
    from src.plugins.word_pulse.analysis import classify_message
    char_pool = {"跌", "亏"}
    cluster_terms = {"涨停": {"涨停"}}
    assert classify_message("又跌了心态崩了", char_pool, cluster_terms) == "GREY"


def test_classify_message_multi_cluster_hit(app: App) -> None:
    """消息同时命中多个 cluster，返回所有命中的。"""
    from src.plugins.word_pulse.analysis import classify_message
    char_pool = {"茅", "五"}
    cluster_terms = {
        "茅台": {"茅台", "茅子"},
        "五粮液": {"五粮液"},
    }
    msg = "茅子和五粮液都涨"
    hits = classify_message(msg, char_pool, cluster_terms)
    assert hits is not None
    assert set(hits) == {"茅台", "五粮液"}


# ── classify_batch prompt 是否带 alias（白盒验证 ai.py 调用参数）──


@pytest.mark.asyncio
async def test_classify_batch_prompt_includes_aliases(app: App) -> None:
    """GREY 阶段 LLM 调用时，cluster 列表应带别名提示。"""
    from src.plugins.word_pulse.ai import classify_batch

    captured_messages: list[list[dict]] = []

    async def fake_request(*, messages, **_):
        captured_messages.append(messages)
        return {"results": [{"id": 1, "cluster": "茅台"}]}

    with patch("src.plugins.word_pulse.ai._request_llm", new=AsyncMock(side_effect=fake_request)):
        await classify_batch(
            base_url="x", api_key="x", model="x",
            messages=[(1, "今天茅子涨疯了")],
            clusters=[{"name": "茅台", "aliases": ["茅子", "飞天"]}],
            theme_name="炒股",
        )

    assert len(captured_messages) == 1
    user_msg = captured_messages[0][1]["content"]  # system=0, user=1
    # cluster 描述里必须出现"茅子""飞天"作为"茅台"的别名
    assert "茅子" in user_msg
    assert "飞天" in user_msg
    assert "茅台" in user_msg


@pytest.mark.asyncio
async def test_classify_batch_prompt_omits_aliases_section_when_empty(app: App) -> None:
    """cluster 没有 alias 时，prompt 不应出现"别名"字样。"""
    from src.plugins.word_pulse.ai import classify_batch

    captured_messages: list[list[dict]] = []

    async def fake_request(*, messages, **_):
        captured_messages.append(messages)
        return {"results": [{"id": 1, "cluster": None}]}

    with patch("src.plugins.word_pulse.ai._request_llm", new=AsyncMock(side_effect=fake_request)):
        await classify_batch(
            base_url="x", api_key="x", model="x",
            messages=[(1, "闲聊")],
            clusters=[{"name": "茅台", "aliases": []}],
            theme_name="炒股",
        )

    user_msg = captured_messages[0][1]["content"]
    assert "别名" not in user_msg


# ── bug#3 回归：_request_llm 直接发 json_object，不走 strict→fallback ──


@pytest.mark.asyncio
async def test_request_llm_uses_json_object_not_strict(app: App) -> None:
    """bug#3 回归：_request_llm 直接发 response_format=json_object，不走 strict→fallback。

    通过 httpx.MockTransport 捕获实际请求体，断言：
    1. response_format 是 {"type": "json_object"}（不是 strict json_schema）
    2. 只发一次请求（无降级重试）
    """
    from src.plugins.word_pulse.ai import _request_llm  # noqa: PLC0415

    captured_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_bodies.append(body)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"results": []}'}}]},
        )

    transport = httpx.MockTransport(handler)

    # patch httpx.AsyncClient 以注入 MockTransport（trust_env 仍保留）
    real_async_client = httpx.AsyncClient

    class _PatchedClient(real_async_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    with patch("src.plugins.word_pulse.ai.httpx.AsyncClient", _PatchedClient):
        result = await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )

    assert result == {"results": []}
    assert len(captured_bodies) == 1, "应只发一次请求（无 strict→fallback 降级）"
    assert captured_bodies[0]["response_format"] == {"type": "json_object"}
    assert "strict" not in captured_bodies[0]
