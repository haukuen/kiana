"""word_pulse 插件边界条件补充测试。

与现有测试的差异:
- ``test_word_pulse_analysis.py``:classify_message 基础分支 + classify_batch
  prompt 白盒 + ``_request_llm`` 走 json_object(bug#3)。
- ``test_word_pulse_analysis_extra.py``:analysis.py 的 classify_message /
  uniform_sample / _pick_evenly / merge_results / build_summary_prompt /
  _compute_day_bucket / compute_or_load_buckets 全套边界。
- ``test_word_pulse_commands.py``:parse_command 各 action 正常路径 + help。
- ``test_word_pulse_handlers.py``:handler 端到端。

本文件**只补尚未覆盖的边界**,不重复上述测试:
1. **ai.py 边界(bug#3 相关)** — ``_request_llm`` 各 HTTP 错误码(401/403/404/
   500/429)、timeout、ConnectError(消息含原始异常类型名)、非 JSON 响应、空
   choices、content 非 str、模型输出非合法 JSON;``expand_charsets`` pydantic
   校验(chars<5 / chars>30 / 缺 charsets 字段 / cluster 名字与种子词不匹配);
   ``classify_batch`` 空 messages / 超 max_batch_size 分批;``summarize`` 空 ranking。
2. **commands.py 边界** — ``parse_command`` 缺参数场景;``parse_query`` 各异常;
   ``resolve_window_days`` value=0 / 超限 / 未知 unit。

注意:analysis.py 的边界(classify_message / uniform_sample / _pick_evenly /
merge_results / build_summary_prompt)已被 ``test_word_pulse_analysis_extra.py``
完整覆盖,本文件**不重复**这些(避免「为补边界而补」)。

不修改任何源代码、不修改其他测试。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from nonebug import App

# ═══════════════════════════════════════════════════════════════
# 辅助:构造 httpx 响应 / 错误
# ═══════════════════════════════════════════════════════════════


def _ok_response(content: str) -> httpx.Response:
    """构造 200 + 合法 chat/completions 响应(content 为字符串)。"""
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}]},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    """构造 HTTPStatusError,raise_for_status 时会抛。"""
    resp = httpx.Response(
        status_code,
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=resp.request, response=resp
    )


# ═══════════════════════════════════════════════════════════════
# 1.1 _request_llm:HTTP 错误码 → 异常类型映射
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_request_llm_401_raises_auth_error(app: App) -> None:
    """HTTP 401 → WordPulseAIAuthError(ai.py:140-141)。"""
    from src.plugins.word_pulse.ai import WordPulseAIAuthError, _request_llm

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=_http_error(401)),
    ), pytest.raises(WordPulseAIAuthError):
        await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )


@pytest.mark.asyncio
async def test_request_llm_403_raises_auth_error(app: App) -> None:
    """HTTP 403 → WordPulseAIAuthError(ai.py:140-141)。"""
    from src.plugins.word_pulse.ai import WordPulseAIAuthError, _request_llm

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=_http_error(403)),
    ), pytest.raises(WordPulseAIAuthError):
        await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )


@pytest.mark.asyncio
async def test_request_llm_404_raises_service_error(app: App) -> None:
    """HTTP 404 → WordPulseAIServiceError(ai.py:142,非 401/403 走默认分支)。"""
    from src.plugins.word_pulse.ai import WordPulseAIServiceError, _request_llm

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=_http_error(404)),
    ), pytest.raises(WordPulseAIServiceError) as exc_info:
        await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )
    assert "404" in str(exc_info.value)


@pytest.mark.asyncio
async def test_request_llm_500_raises_service_error(app: App) -> None:
    """HTTP 500 → WordPulseAIServiceError。"""
    from src.plugins.word_pulse.ai import WordPulseAIServiceError, _request_llm

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=_http_error(500)),
    ), pytest.raises(WordPulseAIServiceError) as exc_info:
        await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )
    assert "500" in str(exc_info.value)


@pytest.mark.asyncio
async def test_request_llm_429_raises_service_error(app: App) -> None:
    """HTTP 429(限流)→ WordPulseAIServiceError。

    边界:429 不在 {401, 403},走默认 service error 分支。
    ai.py 的 _to_ai_error 未对 429 特化处理(不限流重试)。
    """
    from src.plugins.word_pulse.ai import WordPulseAIServiceError, _request_llm

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=_http_error(429)),
    ), pytest.raises(WordPulseAIServiceError) as exc_info:
        await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )
    assert "429" in str(exc_info.value)


# ═══════════════════════════════════════════════════════════════
# 1.2 _request_llm:timeout / 网络错误(bug#3 改进点)
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_request_llm_timeout_raises_timeout_error(app: App) -> None:
    """httpx.TimeoutException → WordPulseAITimeoutError(ai.py:117-118)。"""
    from src.plugins.word_pulse.ai import WordPulseAITimeoutError, _request_llm

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=httpx.TimeoutException("read timeout")),
    ), pytest.raises(WordPulseAITimeoutError):
        await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )


@pytest.mark.asyncio
async def test_request_llm_connect_error_message_contains_exception_type(app: App) -> None:
    """httpx.RequestError(ConnectError)→ WordPulseAIServiceError,消息含原始异常类型名。

    bug#3 改进点:ai.py:122 ``f"AI 请求失败: {type(e).__name__}: {e}"``,
    消息必须包含异常类名(ConnectError),便于日志定位是 DNS / 连接 / 代理问题。
    """
    from src.plugins.word_pulse.ai import WordPulseAIServiceError, _request_llm

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=httpx.ConnectError("dns lookup failed")),
    ), pytest.raises(WordPulseAIServiceError) as exc_info:
        await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )
    msg = str(exc_info.value)
    assert "ConnectError" in msg, f"消息应含原始异常类型名,实际: {msg}"
    assert "dns lookup failed" in msg


@pytest.mark.asyncio
async def test_request_llm_read_error_message_contains_exception_type(app: App) -> None:
    """httpx.ReadError(同为 RequestError 子类)→ 消息含 'ReadError' 类型名。

    边界:bug#3 改进点对**所有** RequestError 子类生效,不限于 ConnectError。
    """
    from src.plugins.word_pulse.ai import WordPulseAIServiceError, _request_llm

    with patch(
        "httpx.AsyncClient.post",
        new=AsyncMock(side_effect=httpx.ReadError("connection reset")),
    ), pytest.raises(WordPulseAIServiceError) as exc_info:
        await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )
    assert "ReadError" in str(exc_info.value)


# ═══════════════════════════════════════════════════════════════
# 1.3 _request_llm:响应格式异常
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_request_llm_non_json_response_raises_response_error(app: App) -> None:
    """响应 body 不是合法 JSON → WordPulseAIResponseError(ai.py:124-127)。"""
    from src.plugins.word_pulse.ai import WordPulseAIResponseError, _request_llm

    response = httpx.Response(
        200,
        text="<html>not json</html>",
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)),
        pytest.raises(WordPulseAIResponseError) as exc_info,
    ):
        await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )
    assert "不是合法 JSON" in str(exc_info.value)


@pytest.mark.asyncio
async def test_request_llm_empty_choices_raises_response_error(app: App) -> None:
    """响应 JSON 但 choices 为空 list → WordPulseAIResponseError。

    边界:_extract_content(ai.py:82-84)``not choices`` → 抛「响应中缺少 choices」。
    """
    from src.plugins.word_pulse.ai import WordPulseAIResponseError, _request_llm

    response = httpx.Response(
        200,
        json={"choices": []},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)),
        pytest.raises(WordPulseAIResponseError),
    ):
        await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )


@pytest.mark.asyncio
async def test_request_llm_missing_choices_key_raises_response_error(app: App) -> None:
    """响应 JSON 顶层无 choices key → WordPulseAIResponseError。"""
    from src.plugins.word_pulse.ai import WordPulseAIResponseError, _request_llm

    response = httpx.Response(
        200,
        json={"id": "xxx", "object": "chat.completion"},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)),
        pytest.raises(WordPulseAIResponseError),
    ):
        await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )


@pytest.mark.asyncio
async def test_request_llm_content_not_string_raises_response_error(app: App) -> None:
    """响应 content 不是 str/list(此处要求 str)→ WordPulseAIResponseError。

    边界:_extract_content(ai.py:88-91)``if not isinstance(content, str)`` → 抛。
    word_pulse 的 _extract_content 比 refine 更严格:不接受 array content,
    只接受 str。
    """
    from src.plugins.word_pulse.ai import WordPulseAIResponseError, _request_llm

    # content 是 list(refine 接受,但 word_pulse 拒绝)
    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": [{"type": "text", "text": "x"}]}}]},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)),
        pytest.raises(WordPulseAIResponseError),
    ):
        await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )


@pytest.mark.asyncio
async def test_request_llm_content_none_raises_response_error(app: App) -> None:
    """响应 content 为 None → WordPulseAIResponseError。"""
    from src.plugins.word_pulse.ai import WordPulseAIResponseError, _request_llm

    response = httpx.Response(
        200,
        json={"choices": [{"message": {"content": None}}]},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )
    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)),
        pytest.raises(WordPulseAIResponseError),
    ):
        await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )


# ═══════════════════════════════════════════════════════════════
# 1.4 _request_llm:content(模型输出)JSON 解析
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_request_llm_valid_json_content_returns_dict(app: App) -> None:
    """模型输出 content 是合法 JSON 字符串 → 解析为 dict 返回。

    边界:ai.py:132-135,``json.loads(content)`` 成功,返回 dict。
    """
    from src.plugins.word_pulse.ai import _request_llm

    payload = {"results": [{"id": 1, "cluster": "茅台"}]}
    response = _ok_response(json.dumps(payload, ensure_ascii=False))
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
        result = await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )
    assert result == payload


@pytest.mark.asyncio
async def test_request_llm_invalid_json_content_raises_response_error(app: App) -> None:
    """模型输出 content 不是合法 JSON → WordPulseAIResponseError。

    边界:ai.py:132-135,``json.loads(content)`` 抛 JSONDecodeError →
    转 WordPulseAIResponseError「模型输出不是合法 JSON」。
    这是 word_pulse 比 refine 多的一层校验(refine 直接返回原文,
    word_pulse 要求 content 本身是 JSON)。
    """
    from src.plugins.word_pulse.ai import WordPulseAIResponseError, _request_llm

    response = _ok_response("这不是 JSON 格式的纯文本")
    with (
        patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)),
        pytest.raises(WordPulseAIResponseError) as exc_info,
    ):
        await _request_llm(
            base_url="https://example.com", api_key="k", model="m",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0, timeout_seconds=10.0,
        )
    assert "模型输出不是合法 JSON" in str(exc_info.value)


# ═══════════════════════════════════════════════════════════════
# 1.5 expand_charsets:pydantic schema 校验
# ═══════════════════════════════════════════════════════════════


def _build_expand_charsets_patch(returned_json: dict):
    """patch _request_llm 让 expand_charsets 拿到指定的 parsed dict。

    expand_charsets 内部 try CharsetExpansionResponse.model_validate(parsed),
    我们绕过 HTTP 直接喂 parsed,聚焦 pydantic 校验边界。
    """
    return patch(
        "src.plugins.word_pulse.ai._request_llm",
        new=AsyncMock(return_value=returned_json),
    )


@pytest.mark.asyncio
async def test_expand_charsets_chars_less_than_5_raises(app: App) -> None:
    """chars 数组长度 < 5 → WordPulseAIResponseError。

    边界:CharsetItem.chars = Field(min_length=5)(ai.py:32),少于 5 个字符
    触发 ValidationError → expand_charsets 转 WordPulseAIResponseError。
    """
    from src.plugins.word_pulse.ai import WordPulseAIResponseError, expand_charsets

    with _build_expand_charsets_patch(
        {"charsets": [{"cluster": "茅台", "chars": ["茅", "台", "酒"]}]}
    ), pytest.raises(WordPulseAIResponseError) as exc_info:
        await expand_charsets(
            base_url="x", api_key="x", model="x",
            seeds=["茅台"], theme="炒股",
        )
    assert "格式异常" in str(exc_info.value)


@pytest.mark.asyncio
async def test_expand_charsets_chars_more_than_30_raises(app: App) -> None:
    """chars 数组长度 > 30 → WordPulseAIResponseError。

    边界:CharsetItem.chars = Field(max_length=30)(ai.py:32)。
    """
    from src.plugins.word_pulse.ai import WordPulseAIResponseError, expand_charsets

    too_many = [f"字{i}" for i in range(31)]
    with _build_expand_charsets_patch(
        {"charsets": [{"cluster": "茅台", "chars": too_many}]}
    ), pytest.raises(WordPulseAIResponseError):
        await expand_charsets(
            base_url="x", api_key="x", model="x",
            seeds=["茅台"], theme="炒股",
        )


@pytest.mark.asyncio
async def test_expand_charsets_missing_charsets_field_raises(app: App) -> None:
    """charsets 字段缺失 → WordPulseAIResponseError。

    边界:CharsetExpansionResponse 要求 charsets 必填(ai.py:36),
    缺字段触发 ValidationError。
    """
    from src.plugins.word_pulse.ai import WordPulseAIResponseError, expand_charsets

    with _build_expand_charsets_patch({"results": []}), pytest.raises(
        WordPulseAIResponseError
    ):
        await expand_charsets(
            base_url="x", api_key="x", model="x",
            seeds=["茅台"], theme="炒股",
        )


@pytest.mark.asyncio
async def test_expand_charsets_cluster_name_mismatch_seed_still_succeeds(app: App) -> None:
    """cluster 名字与种子词不匹配 → 仍能成功(pydantic 不强校验名字一致)。

    边界:CharsetItem 只校验 cluster 是 str + chars 长度,不校验 cluster
    必须出现在 seeds 列表里。LLM 返回的 cluster 名字与种子词不一致时,
    expand_charsets 仍返回 dict(只是 key 是 LLM 给的名字)。
    这是设计上的容错:LLM 可能返回种子词的变体(如「茅台酒」vs「茅台」)。
    """
    from src.plugins.word_pulse.ai import expand_charsets

    with _build_expand_charsets_patch(
        {"charsets": [
            {"cluster": "完全无关的名字", "chars": ["茅", "台", "酒", "业", "股"]},
        ]}
    ):
        result = await expand_charsets(
            base_url="x", api_key="x", model="x",
            seeds=["茅台"], theme="炒股",
        )
    # key 是 LLM 给的名字,不是种子词
    assert "完全无关的名字" in result
    assert result["完全无关的名字"] == ["茅", "台", "酒", "业", "股"]


# ═══════════════════════════════════════════════════════════════
# 1.6 classify_batch / summarize 边界
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_classify_batch_empty_messages_returns_empty_without_llm_call(
    app: App,
) -> None:
    """messages 为空 → 直接返回 [],不调 _request_llm。

    边界:ai.py:194-196,``if not messages: return []``,短路避免无意义 LLM 调用。
    """
    from src.plugins.word_pulse.ai import classify_batch

    with patch(
        "src.plugins.word_pulse.ai._request_llm",
        new=AsyncMock(side_effect=AssertionError("空 messages 不该调 LLM")),
    ):
        result = await classify_batch(
            base_url="x", api_key="x", model="x",
            messages=[], clusters=[{"name": "茅台"}], theme_name="炒股",
        )
    assert result == []


@pytest.mark.asyncio
async def test_classify_batch_over_max_batch_size_splits_into_chunks(app: App) -> None:
    """messages 数量超过 max_batch_size → 分批处理,结果合并。

    边界:ai.py:201-215,``for start in range(0, len(messages), max_batch_size)``
    分块调用 _request_llm,extend 合并结果。本用例发 5 条,max_batch_size=2,
    应分 3 批(2+2+1),共调 3 次 LLM,返回 5 条结果。
    """
    from src.plugins.word_pulse.ai import classify_batch

    call_count = 0

    async def fake_request(*, messages, **_):
        nonlocal call_count
        call_count += 1
        # 从 user message 里解析 [id] 还原 results
        user_msg = messages[1]["content"]
        ids = []
        for line in user_msg.split("\n"):
            if line.startswith("[") and "]" in line:
                id_str = line[1:line.index("]")]
                ids.append(int(id_str))
        return {"results": [{"id": mid, "cluster": "茅台"} for mid in ids]}

    with patch("src.plugins.word_pulse.ai._request_llm", new=AsyncMock(side_effect=fake_request)):
        result = await classify_batch(
            base_url="x", api_key="x", model="x",
            messages=[(i, f"消息{i}") for i in range(1, 6)],
            clusters=[{"name": "茅台"}], theme_name="炒股",
            max_batch_size=2,
        )

    assert call_count == 3, f"5 条按 max_batch_size=2 应分 3 批,实际 {call_count} 批"
    assert len(result) == 5
    # 所有 id 都被分类
    result_ids = {mid for mid, _ in result}
    assert result_ids == {1, 2, 3, 4, 5}
    # 所有都归到茅台
    assert all(cluster == "茅台" for _, cluster in result)


@pytest.mark.asyncio
async def test_summarize_empty_ranking_still_succeeds(app: App) -> None:
    """summarize 返回 ranking 为空 → 仍能成功(空主题/无命中的合法形态)。

    边界:SummaryResult.ranking 是 list[RankItem](ai.py:67),允许空 list
    (无 min_length 约束)。LLM 可能对「无任何 cluster 命中」的主题返回空 ranking。
    """
    from src.plugins.word_pulse.ai import SummaryResult, summarize

    empty_payload = {
        "ranking": [],
        "trend": "本周期内该主题无讨论",
        "examples": [],
        "unclassified_high_freq": [],
    }
    with patch(
        "src.plugins.word_pulse.ai._request_llm",
        new=AsyncMock(return_value=empty_payload),
    ):
        result = await summarize(
            base_url="x", api_key="x", model="x", prompt="随便",
        )
    assert isinstance(result, SummaryResult)
    assert result.ranking == []
    assert result.trend == "本周期内该主题无讨论"


# ═══════════════════════════════════════════════════════════════
# 2. commands.py 边界:parse_command 缺参数场景
# ═══════════════════════════════════════════════════════════════
#
# test_word_pulse_commands.py 已覆盖各 action 正常路径 + unknown/empty。
# 这里补缺参数的边界形态。
# ═══════════════════════════════════════════════════════════════


def test_parse_command_bare_prefix_returns_none(app: App) -> None:
    r"""``"词频"``(仅命令前缀,无 action)→ None。

    边界:commands.py:36-66,所有正则都要求 ``词频\s+action``,仅「词频」不匹配任何。
    """
    from src.plugins.word_pulse.commands import parse_command

    assert parse_command("词频") is None


def test_parse_command_add_missing_seeds_returns_none(app: App) -> None:
    r"""``"词频 add 主题"``(缺种子词)→ None。

    边界:_CMD_ADD = ``词频\s+add\s+(\S+)\s+(.+)$``(commands.py:24),
    主题后必须有 ``\s+(.+)`` 捕获种子词,只有主题时不匹配。
    """
    from src.plugins.word_pulse.commands import parse_command

    assert parse_command("词频 add 炒股") is None


def test_parse_command_unknown_action_returns_none(app: App) -> None:
    """``"词频 unknown"``(未知 action)→ None。

    边界:不在 {add/append/list/del/refresh/alias/unalias} 中,所有正则都不匹配。
    """
    from src.plugins.word_pulse.commands import parse_command

    assert parse_command("词频 unknown") is None
    assert parse_command("词频 foobar arg1 arg2") is None


# ═══════════════════════════════════════════════════════════════
# 3. commands.py 边界:parse_query 异常输入
# ═══════════════════════════════════════════════════════════════
#
# _QUERY_RE = ``总结\s+(\d+)\s*(天|d|周|w|月|m)\s+(\S+)$`` (commands.py:33)
# ═══════════════════════════════════════════════════════════════


def test_parse_query_bare_prefix_returns_none(app: App) -> None:
    """``"总结"``(仅前缀)→ None。"""
    from src.plugins.word_pulse.commands import parse_query

    assert parse_query("总结") is None


def test_parse_query_missing_unit_returns_none(app: App) -> None:
    """``"总结 1"``(缺单位)→ None。

    边界:正则要求数字后跟单位 token(天/d/周/w/月/m),只有数字不匹配。
    """
    from src.plugins.word_pulse.commands import parse_query

    assert parse_query("总结 1") is None


def test_parse_query_non_numeric_value_returns_none(app: App) -> None:
    r"""``"总结 x 天 主题"``(非数字 value)→ None。

    边界:正则要求 ``(\d+)``,x 非数字不匹配。
    """
    from src.plugins.word_pulse.commands import parse_query

    assert parse_query("总结 x 天 炒股") is None


def test_parse_query_missing_theme_returns_none(app: App) -> None:
    r"""``"总结 1天"``(缺主题)→ None。

    边界:正则要求单位后 ``\s+(\S+)`` 捕获主题,只有「1天」不匹配。
    """
    from src.plugins.word_pulse.commands import parse_query

    assert parse_query("总结 1天") is None


def test_parse_query_unknown_unit_returns_none(app: App) -> None:
    """``"总结 1 年 主题"``(未知单位「年」)→ None。

    边界:正则单位组只接受 ``天|d|周|w|月|m``,「年」不匹配。
    """
    from src.plugins.word_pulse.commands import parse_query

    assert parse_query("总结 1 年 炒股") is None


def test_parse_query_valid_all_units_parsed(app: App) -> None:
    """合法查询:验证所有 4 种单位 + 数字 + 主题都被正确解析(对照用例)。

    边界:确保上面的「None」用例不是因为正则本身有 bug。
    """
    from src.plugins.word_pulse.commands import parse_query

    for unit in ("天", "d", "周", "w", "月", "m"):
        q = parse_query(f"总结 3{unit} 炒股")
        assert q is not None, f"单位 {unit} 应被解析"
        assert q.window_value == 3
        assert q.window_unit == unit
        assert q.theme == "炒股"


# ═══════════════════════════════════════════════════════════════
# 4. commands.py 边界:resolve_window_days
# ═══════════════════════════════════════════════════════════════


def test_resolve_window_days_zero_value_returns_zero(app: App) -> None:
    """value=0 → 总天数 0(查询当天)。

    边界:commands.py:80-81,multiplier * 0 = 0,不超 max_days,返回 0。
    """
    from src.plugins.word_pulse.commands import resolve_window_days

    assert resolve_window_days(0, "天", max_days=90) == 0


def test_resolve_window_days_exceeding_max_returns_none(app: App) -> None:
    """value=9999 超过 max_days → None。

    边界:commands.py:81,``total > max_days`` 时返回 None,handler 用此判断
    拒绝超期查询。
    """
    from src.plugins.word_pulse.commands import resolve_window_days

    assert resolve_window_days(9999, "天", max_days=90) is None
    # 月单位:9999 * 31 必然超限
    assert resolve_window_days(9999, "月", max_days=90) is None


def test_resolve_window_days_unknown_unit_returns_none(app: App) -> None:
    """未知 unit → None。

    边界:commands.py:77-79,multiplier 字典 .get(unknown_unit) 返回 None,
    ``if multiplier is None: return None``。
    """
    from src.plugins.word_pulse.commands import resolve_window_days

    assert resolve_window_days(7, "年", max_days=90) is None
    assert resolve_window_days(7, "century", max_days=90) is None


def test_resolve_window_days_unit_multipliers(app: App) -> None:
    """验证单位 → 天数 multiplier 正确(对照用例)。

    边界:{天:1, d:1, 周:7, w:7, 月:31, m:31}。
    """
    from src.plugins.word_pulse.commands import resolve_window_days

    assert resolve_window_days(2, "天", max_days=90) == 2
    assert resolve_window_days(2, "d", max_days=90) == 2
    assert resolve_window_days(2, "周", max_days=90) == 14
    assert resolve_window_days(2, "w", max_days=90) == 14
    assert resolve_window_days(1, "月", max_days=90) == 31
    assert resolve_window_days(1, "m", max_days=90) == 31


def test_resolve_window_days_boundary_exactly_max(app: App) -> None:
    """value*multiplier 刚好等于 max_days → 不算超限,返回该值。

    边界:commands.py:81 用 ``>`` 而非 ``>=``,所以刚好等于 max_days 时合法。
    """
    from src.plugins.word_pulse.commands import resolve_window_days

    # 90 天刚好等于 max_days=90
    assert resolve_window_days(90, "天", max_days=90) == 90
    # 91 天超限
    assert resolve_window_days(91, "天", max_days=90) is None
