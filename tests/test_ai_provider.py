"""ai_provider 前置插件：模型路由、三种协议经官方 SDK 的请求与响应、严格结构化输出、自适应探测、「不含功能」约束。

协议测试通过 ``tests.ai_mock_transport`` 注入 ``httpx2.MockTransport``：真实 SDK
完成序列化、反序列化与异常构造，断言落在可观察的请求/结果上，而不是旧手写
adapter 的内部字典。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx2
import pytest
from tests.ai_mock_transport import (
    AIHttpMock,
    AI_HTTP_TARGET,
    chat_ok,
    mock_ai_http,
    status_error,
    transport_failure,
)

PLUGIN_ROOT = Path(__file__).parents[1] / "src" / "plugins" / "ai_provider"
_SOURCE_FILES = sorted(PLUGIN_ROOT.glob("*.py"))


# ── 辅助 ────────────────────────────────────────────────


def _provider(**overrides) -> dict:
    base = {
        "id": "fake",
        "protocol": "openai_chat",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-1",
        "models": [],
    }
    base.update(overrides)
    return base


def _install(
    *providers: dict,
    default_model: str = "fake/gpt-test",
    plugin_models: dict | None = None,
) -> None:
    from src.plugins.ai_provider.config import config as ai_config

    ai_config.ai_providers = list(providers) or [_provider()]
    ai_config.ai_default_model = default_model
    ai_config.ai_plugin_models = plugin_models or {}


async def _call(**overrides):
    from src.plugins.ai_provider import complete

    kwargs: dict = {
        "caller": "fake-plugin",
        "system": "sys",
        "messages": [{"role": "user", "content": "hi"}],
        "timeout_seconds": 10,
    }
    kwargs.update(overrides)
    return await complete(**kwargs)


def _rating_model() -> type:
    from pydantic import BaseModel

    class Rating(BaseModel):
        score: int

    return Rating


# ── 模型路由 ────────────────────────────────────────────


def test_resolve_reports_missing_providers() -> None:
    from src.plugins.ai_provider import resolve
    from src.plugins.ai_provider.config import config as ai_config

    ai_config.ai_providers = []
    ai_config.ai_default_model = ""
    ai_config.ai_plugin_models = {}

    assert resolve("refine").missing == ("ai_providers 未配置",)


def test_resolve_prefers_plugin_model_then_explicit_then_default() -> None:
    from src.plugins.ai_provider import resolve

    _install(
        _provider(models=["m1", "m2"]),
        default_model="fake/m1",
        plugin_models={"refine": "fake/m2"},
    )

    assert resolve("refine").model == "m2"
    assert resolve("refine", "fake/m1").model == "m1"
    assert resolve("gold").model == "m1"


def test_resolve_reports_actionable_problems() -> None:
    from src.plugins.ai_provider import resolve

    _install(_provider(api_key="", models=["gpt-test"]))
    assert resolve("gold").missing == ("provider「fake」缺少 api_key",)

    _install(_provider(models=["m1"]))
    assert resolve("gold").missing == ("provider「fake」的 models 清单里没有「gpt-test」",)
    assert resolve("gold", "fake/m1").complete
    assert "provider「nope」" in resolve("gold", "nope/m1").missing[0]
    assert "provider_id/model_name" in resolve("gold", "just-a-model").missing[0]
    _install(_provider(), default_model="")
    assert "ai_default_model" in resolve("gold").missing[0]


def test_config_rejects_empty_and_duplicate_provider_ids() -> None:
    from pydantic import ValidationError

    from src.plugins.ai_provider.config import Config

    with pytest.raises(ValidationError, match="id 不能为空"):
        Config(ai_providers=[_provider(id=" ")])
    with pytest.raises(ValidationError, match="重复 id"):
        Config(
            ai_providers=[
                _provider(id="same"),
                _provider(id="same", base_url="https://other.example.com/v1"),
            ]
        )


def test_usable_urls_cover_per_protocol_endpoints() -> None:
    from src.plugins.ai_provider import usable_urls
    from src.plugins.ai_provider.config import ProviderConfig

    fixed = ProviderConfig(**_provider(protocol="anthropic_messages"))
    assert list(usable_urls(fixed)) == ["anthropic_messages"]

    auto = ProviderConfig(
        **_provider(
            protocol="auto",
            base_url="https://relay.example.com/v1",
            anthropic_base_url="https://api.anthropic.com/v1",
        )
    )
    urls = usable_urls(auto)
    assert urls["anthropic_messages"] == "https://api.anthropic.com/v1"
    assert urls["openai_chat"] == "https://relay.example.com/v1"
    # 没单独配端点的协议回落到主端点：同一个中转可能就是三种协议都吃
    assert urls["openai_responses"] == "https://relay.example.com/v1"


def test_temperature_defaults_per_protocol() -> None:
    from src.plugins.ai_provider import provider_sends_temperature
    from src.plugins.ai_provider.config import ProviderConfig

    def sends(protocol: str, policy: str):
        provider = ProviderConfig(**_provider(protocol=protocol, temperature_policy=policy))
        return provider_sends_temperature(provider, protocol)  # type: ignore[arg-type]

    # auto 对各协议都保守省略（推理模型收到 temperature 会 400）
    assert sends("openai_chat", "auto") is False
    assert sends("openai_responses", "auto") is False
    assert sends("anthropic_messages", "auto") is False
    assert sends("anthropic_messages", "send") is True
    assert sends("openai_chat", "drop") is False


# ── openai_chat ─────────────────────────────────────────


async def test_openai_chat_sends_expected_body_and_returns_content() -> None:
    _install(_provider(temperature_policy="send"))

    with mock_ai_http(chat_ok()) as http:
        result = await _call(json_object=True, temperature=0.3)

    assert result.text == "总结内容"
    assert result.protocol == "openai_chat"
    assert result.provider_id == "fake"
    assert result.model == "gpt-test"
    assert result.usage == {"input_tokens": 3, "output_tokens": 5}
    assert http.urls == ["https://api.example.com/v1/chat/completions"]
    assert http.trust_env_flags == [False], "必须绕开宿主机的环境变量代理"
    request = http.requests[0]
    assert request.headers["Authorization"] == "Bearer sk-1"
    body = json.loads(request.content)
    assert body["model"] == "gpt-test"
    assert body["messages"][1:] == [{"role": "user", "content": "hi"}]
    assert "JSON" in body["messages"][0]["content"], "json_object 要求上下文含 JSON 字样"
    assert body["temperature"] == 0.3
    assert body["response_format"] == {"type": "json_object"}
    assert body["store"] is False, "群聊内容不该留在上游"
    assert body["max_completion_tokens"] == 4096, "新版 OpenAI 协议用 max_completion_tokens"
    assert "max_tokens" not in body


async def test_openai_chat_omits_optional_fields_when_not_set() -> None:
    _install(_provider())

    with mock_ai_http(chat_ok()) as http:
        await _call(system="")

    assert http.bodies() == [
        {
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hi"}],
            "store": False,
            "max_completion_tokens": 4096,
        }
    ], "temperature / response_format 等可选参数必须缺席而不是 null"


async def test_openai_chat_response_model_uses_official_strict_format() -> None:
    _install(_provider())
    rating = _rating_model()

    with mock_ai_http(chat_ok(content='{"score": 61}')) as http:
        result = await _call(response_model=rating)

    assert result.text == '{"score": 61}'
    assert result.parsed == rating(score=61), "SDK parse 直接返回 Pydantic 实例"
    body = json.loads(http.requests[0].content)
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "Rating",
            "schema": {
                "properties": {"score": {"title": "Score", "type": "integer"}},
                "required": ["score"],
                "title": "Rating",
                "type": "object",
                "additionalProperties": False,
            },
            "strict": True,
        },
    }
    assert "json_object" not in json.dumps(body["response_format"])


async def test_openai_chat_response_model_invalid_json_is_response_error() -> None:
    from src.plugins.ai_provider import AIResponseError

    _install(_provider())

    with (
        mock_ai_http(chat_ok(content="不是 JSON")),
        pytest.raises(AIResponseError, match="字段不完整"),
    ):
        await _call(response_model=_rating_model())


async def test_openai_chat_response_model_validator_failure_is_response_error() -> None:
    """SDK 解析时执行 Pydantic 字段约束；越界结果走响应错误，绝不伪装成功。"""
    from pydantic import BaseModel, Field

    from src.plugins.ai_provider import AIResponseError

    class Bounded(BaseModel):
        score: int = Field(ge=0, le=100)

    _install(_provider())
    with (
        mock_ai_http(chat_ok(content='{"score": 150}')),
        pytest.raises(AIResponseError, match="字段不完整"),
    ):
        await _call(response_model=Bounded)


async def test_openai_chat_legacy_token_field_for_old_relays() -> None:
    _install(_provider(chat_token_field="max_tokens"))

    with mock_ai_http(chat_ok()) as http:
        await _call()

    body = json.loads(http.requests[0].content)
    assert body["max_tokens"] == 4096
    assert "max_completion_tokens" not in body


async def test_openai_chat_parses_array_content() -> None:
    """部分中转把 content 放在内容块数组里：最小兼容读取 text 段（SDK 类型只认 str）。"""
    _install(_provider())
    array_reply = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": " 第一段 "},
                        {"type": "image_url", "image_url": {}},
                        {"type": "text", "text": "第二段 "},
                    ]
                }
            }
        ]
    }

    with mock_ai_http(lambda request: httpx2.Response(200, json=array_reply)) as http:
        result = await _call()

    assert result.text == "第一段 第二段"
    assert json.loads(http.requests[0].content)["store"] is False


async def test_openai_chat_empty_content_is_strict_failure() -> None:
    from src.plugins.ai_provider import AIResponseError

    _install(_provider())
    with (
        mock_ai_http(chat_ok(content="")),
        pytest.raises(AIResponseError, match="正文为空"),
    ):
        await _call()


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "tool_calls", "function_call"])
async def test_openai_chat_bad_finish_reasons_raise(finish_reason: str) -> None:
    from src.plugins.ai_provider import AIResponseError

    _install(_provider())
    with (
        mock_ai_http(chat_ok(content="部分", finish_reason=finish_reason)),
        pytest.raises(AIResponseError, match=finish_reason),
    ):
        await _call()


async def test_openai_chat_refusal_raises_even_with_content() -> None:
    from src.plugins.ai_provider import AIResponseError

    _install(_provider())
    refusal_reply = {
        "choices": [
            {"message": {"content": None, "refusal": "抱歉，我无法回答"}, "finish_reason": "stop"}
        ]
    }
    with (
        mock_ai_http(lambda request: httpx2.Response(200, json=refusal_reply)),
        pytest.raises(AIResponseError, match="拒绝回答"),
    ):
        await _call()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": ["not-a-dict"]},
        {"choices": [{}]},
        {"choices": [{"message": {"content": {"unexpected": "dict"}}}]},
    ],
)
async def test_openai_chat_unparsable_content_raises(payload: dict) -> None:
    from src.plugins.ai_provider import AIResponseError

    _install(_provider())
    with (
        mock_ai_http(lambda request: httpx2.Response(200, json=payload)),
        pytest.raises(AIResponseError),
    ):
        await _call()


# ── anthropic_messages ──────────────────────────────────


async def test_anthropic_messages_uses_native_wire_format() -> None:
    _install(
        _provider(
            protocol="anthropic_messages",
            base_url="https://api.anthropic.com/v1",
            max_tokens=8192,
        )
    )
    reply = {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "model": "gpt-test",
        "content": [
            {"type": "thinking", "thinking": "推理过程"},
            {"type": "text", "text": "第一段"},
            {"type": "text", "text": "第二段"},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 2, "output_tokens": 7},
    }

    with mock_ai_http(lambda request: httpx2.Response(200, json=reply)) as http:
        result = await _call(json_object=True, temperature=0.3)

    assert result.text == "第一段第二段", "thinking 块要跳过，text 块按顺序拼接"
    assert result.protocol == "anthropic_messages"
    assert result.usage == {"input_tokens": 2, "output_tokens": 7}
    # 项目 base_url 约定含 /v1，SDK 自己追加 v1/messages：不得出现 /v1/v1/messages
    assert http.urls == ["https://api.anthropic.com/v1/messages"]
    request = http.requests[0]
    assert request.headers["x-api-key"] == "sk-1"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in request.headers
    body = json.loads(request.content)
    assert body["max_tokens"] == 8192
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["system"].startswith("sys"), "system 是顶层字段"
    assert "JSON" in body["system"], "json_object 用提示词约束兜底"
    assert all(message["role"] != "system" for message in body["messages"])
    assert "temperature" not in body, "官方 SDK 已移除 Anthropic 的 temperature 参数"


async def test_anthropic_messages_never_sends_temperature_even_with_send_policy() -> None:
    """temperature_policy=send 对 Anthropic 无效：SDK 不再支持该参数，宁缺毋错。"""
    _install(_provider(protocol="anthropic_messages", temperature_policy="send"))
    reply = {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "model": "gpt-test",
        "content": [{"type": "text", "text": "x"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }

    with mock_ai_http(lambda request: httpx2.Response(200, json=reply)) as http:
        await _call(temperature=0.2)

    assert "temperature" not in json.loads(http.requests[0].content)


async def test_anthropic_messages_rejects_content_without_text_block() -> None:
    from src.plugins.ai_provider import AIResponseError

    _install(_provider(protocol="anthropic_messages"))
    reply = {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "model": "gpt-test",
        "content": [{"type": "thinking", "thinking": "x"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    with (
        mock_ai_http(lambda request: httpx2.Response(200, json=reply)),
        pytest.raises(AIResponseError),
    ):
        await _call()


async def test_anthropic_messages_response_model_uses_official_output_config() -> None:
    _install(_provider(protocol="anthropic_messages"))
    rating = _rating_model()
    reply = {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "model": "gpt-test",
        "content": [{"type": "text", "text": '{"score": 61}'}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }

    with mock_ai_http(lambda request: httpx2.Response(200, json=reply)) as http:
        result = await _call(response_model=rating)

    assert result.text == '{"score": 61}'
    assert result.parsed == rating(score=61)
    body = json.loads(http.requests[0].content)
    assert body["output_config"] == {
        "format": {
            "type": "json_schema",
            "schema": {
                "properties": {"score": {"title": "Score", "type": "integer"}},
                "required": ["score"],
                "title": "Rating",
                "type": "object",
                "additionalProperties": False,
            },
        }
    }
    assert "response_format" not in body
    assert "text" not in body


@pytest.mark.parametrize(
    "stop_reason",
    ["max_tokens", "model_context_window_exceeded", "refusal", "tool_use", "pause_turn"],
)
async def test_anthropic_messages_bad_stop_reasons_raise(stop_reason: str) -> None:
    from src.plugins.ai_provider import AIResponseError

    _install(_provider(protocol="anthropic_messages"))
    reply = {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "model": "gpt-test",
        "content": [{"type": "text", "text": "部分"}],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    with (
        mock_ai_http(lambda request: httpx2.Response(200, json=reply)),
        pytest.raises(AIResponseError, match=stop_reason),
    ):
        await _call()


async def test_anthropic_messages_schema_output_requires_complete_end_turn() -> None:
    from src.plugins.ai_provider import AIResponseError

    _install(_provider(protocol="anthropic_messages"))
    rating = _rating_model()

    def reply(text: str, stop_reason: str | None) -> httpx2.Response:
        payload: dict = {
            "id": "msg-1",
            "type": "message",
            "role": "assistant",
            "model": "gpt-test",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        if stop_reason is not None:
            payload["stop_reason"] = stop_reason
        return httpx2.Response(200, json=payload)

    # 中转省略 stop_reason：普通文本放行，严格 schema 输出必须拒绝（无法证明完整）
    with mock_ai_http(lambda request: reply('{"score": 1}', None)):
        assert (await _call()).text == '{"score": 1}'
    with (
        mock_ai_http(lambda request: reply('{"score": 1}', None)),
        pytest.raises(AIResponseError, match="stop_reason"),
    ):
        await _call(response_model=rating)

    # 未知停止原因同样失败
    with (
        mock_ai_http(lambda request: reply("x", "eos")),
        pytest.raises(AIResponseError, match="eos"),
    ):
        await _call()


async def test_anthropic_messages_openai_2_range_still_validated_for_openai_protocols() -> None:
    from src.plugins.ai_provider import AIConfigError

    _install(_provider(protocol="openai_chat", temperature_policy="send"))
    with pytest.raises(AIConfigError, match="temperature 必须在 0 到 2"):
        await _call(temperature=2.5)


# ── openai_responses ────────────────────────────────────


def _responses_reply(output: list[dict], status: str = "completed", **extra) -> dict:
    return {
        "id": "resp-1",
        "object": "response",
        "created_at": 0,
        "status": status,
        "model": "gpt-test",
        "output": output,
        "usage": {"input_tokens": 4, "output_tokens": 6},
        **extra,
    }


async def test_responses_uses_input_instructions_and_store_false() -> None:
    _install(_provider(protocol="openai_responses", max_tokens=2048))
    reply = _responses_reply(
        [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "id": "m-1",
                "content": [{"type": "output_text", "text": "答案", "annotations": []}],
            },
        ]
    )

    with mock_ai_http(lambda request: httpx2.Response(200, json=reply)) as http:
        result = await _call(json_object=True)

    assert result.text == "答案", "reasoning item 要跳过"
    assert result.usage == {"input_tokens": 4, "output_tokens": 6}
    assert http.urls == ["https://api.example.com/v1/responses"]
    body = json.loads(http.requests[0].content)
    assert body["instructions"].startswith("sys"), "system 放 instructions"
    assert "JSON" in body["instructions"], "json_object 要求上下文含 JSON 字样"
    assert body["input"] == [{"role": "user", "content": "hi"}]
    assert body["store"] is False, "群聊内容不该留在上游"
    assert body["max_output_tokens"] == 2048
    assert body["text"] == {"format": {"type": "json_object"}}


async def test_responses_without_usable_output_raises() -> None:
    from src.plugins.ai_provider import AIResponseError

    _install(_provider(protocol="openai_responses"))
    reply = _responses_reply([{"type": "reasoning", "summary": []}])
    with (
        mock_ai_http(lambda request: httpx2.Response(200, json=reply)),
        pytest.raises(AIResponseError, match="缺少可解析的 output"),
    ):
        await _call()


async def test_responses_response_model_uses_official_text_format() -> None:
    _install(_provider(protocol="openai_responses"))
    rating = _rating_model()
    reply = _responses_reply(
        [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "id": "m-1",
                "content": [
                    {"type": "output_text", "text": '{"score": 61}', "annotations": []}
                ],
            },
        ]
    )

    with mock_ai_http(lambda request: httpx2.Response(200, json=reply)) as http:
        result = await _call(response_model=rating)

    assert result.text == '{"score": 61}'
    assert result.parsed == rating(score=61)
    body = json.loads(http.requests[0].content)
    assert body["text"] == {
        "format": {
            "type": "json_schema",
            "name": "Rating",
            "schema": {
                "properties": {"score": {"title": "Score", "type": "integer"}},
                "required": ["score"],
                "title": "Rating",
                "type": "object",
                "additionalProperties": False,
            },
            "strict": True,
        }
    }


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        pytest.param(
            _responses_reply([], status="failed", error={"message": "上游炸了"}, output_text="残缺文本"),
            "上游炸了",
            id="failed-error-message",
        ),
        pytest.param(
            _responses_reply([], status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
            "截断",
            id="incomplete-max-tokens",
        ),
        pytest.param(
            _responses_reply([], status="incomplete", incomplete_details={"reason": "content_filter"}),
            "安全过滤",
            id="incomplete-content-filter",
        ),
        pytest.param(
            _responses_reply([], status="in_progress"),
            "尚未完成",
            id="in-progress",
        ),
        pytest.param(
            _responses_reply(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "id": "m-1",
                        "content": [{"type": "refusal", "refusal": "不行"}],
                    }
                ]
            ),
            "缺少可解析的 output",
            id="refusal-part-has-no-text",
        ),
    ],
)
async def test_responses_bad_statuses_and_refusal_raise(payload: dict, match: str) -> None:
    from src.plugins.ai_provider import AIResponseError

    _install(_provider(protocol="openai_responses"))
    with (
        mock_ai_http(lambda request: httpx2.Response(200, json=payload)),
        pytest.raises(AIResponseError, match=match),
    ):
        await _call()


async def test_responses_failed_status_gates_before_any_text_extraction() -> None:
    """正文提取永远走不到 failed 响应的残缺字段前面。"""
    from src.plugins.ai_provider import AIResponseError

    _install(_provider(protocol="openai_responses"))
    payload = _responses_reply([], status="failed", error={"message": "boom"}, output_text="看起来能用")
    with (
        mock_ai_http(lambda request: httpx2.Response(200, json=payload)),
        pytest.raises(AIResponseError, match="boom"),
    ):
        await _call()


# ── 错误映射与注入 ──────────────────────────────────────


async def test_timeout_maps_to_timeout_error() -> None:
    from src.plugins.ai_provider import AITimeoutError

    _install(_provider())
    with (
        mock_ai_http(transport_failure(httpx2.ReadTimeout("too slow"))),
        pytest.raises(AITimeoutError),
    ):
        await _call()


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_status_maps_to_auth_error(status: int) -> None:
    from src.plugins.ai_provider import AIAuthError

    _install(_provider())
    with (
        mock_ai_http(status_error(status, body={"error": {"message": "bad key"}})),
        pytest.raises(AIAuthError) as exc_info,
    ):
        await _call()
    assert "HTTP " in str(exc_info.value)


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages", "openai_responses"])
async def test_server_error_maps_to_service_error(protocol: str) -> None:
    from src.plugins.ai_provider import AIServiceError

    _install(_provider(protocol=protocol))
    with (
        mock_ai_http(status_error(500)),
        pytest.raises(AIServiceError) as exc_info,
    ):
        await _call()

    assert "HTTP 500" in str(exc_info.value)


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages", "openai_responses"])
@pytest.mark.parametrize("structured", [False, True])
async def test_malformed_json_body_maps_to_response_error(protocol: str, structured: bool) -> None:
    from src.plugins.ai_provider import AIResponseError

    _install(_provider(protocol=protocol))
    with (
        mock_ai_http(lambda request: httpx2.Response(
            200, content=b"{broken", headers={"content-type": "application/json"},
        )),
        pytest.raises(AIResponseError, match="不是合法 JSON") as exc_info,
    ):
        await _call(response_model=_rating_model() if structured else None)
    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)



async def test_network_error_and_non_json_body_map_to_respective_errors() -> None:
    from src.plugins.ai_provider import AIResponseError, AIServiceError

    _install(_provider())
    with (
        mock_ai_http(transport_failure(httpx2.ConnectError("refused"))),
        pytest.raises(AIServiceError) as exc_info,
    ):
        await _call()
    assert "AI 请求失败" in str(exc_info.value)

    # 200 但正文不是 JSON：SDK 的响应校验异常映射成响应错误
    with (
        mock_ai_http(lambda request: httpx2.Response(200, text="nope")),
        pytest.raises(AIResponseError),
    ):
        await _call()

    # 200 顶层不是对象
    with (
        mock_ai_http(lambda request: httpx2.Response(200, json=["x"])),
        pytest.raises(AIResponseError),
    ):
        await _call()


async def test_injected_error_types_are_used() -> None:
    """调用方注入自己的异常类时，共享层必须抛它们而不是默认层级。"""

    class MyTimeout(Exception):
        pass

    from src.plugins.ai_provider import AIErrorTypes

    _install(_provider())
    errors = AIErrorTypes(
        timeout=MyTimeout,
        auth=MyTimeout,
        service=MyTimeout,
        response=MyTimeout,
    )
    with (
        mock_ai_http(transport_failure(httpx2.ReadTimeout("too slow"))),
        pytest.raises(MyTimeout),
    ):
        await _call(errors=errors)


async def test_complete_without_configuration_raises_config_error() -> None:
    from src.plugins.ai_provider import AIConfigError
    from src.plugins.ai_provider.config import config as ai_config

    ai_config.ai_providers = []
    with pytest.raises(AIConfigError):
        await _call()


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"json_object": True, "response_model": _rating_model()}, id="互斥"),
        pytest.param({"response_model": "不是模型"}, id="response_model 非模型"),
        pytest.param({"response_model": dict}, id="response_model 不是 Pydantic"),
        pytest.param({"messages": []}, id="messages 为空"),
        pytest.param(
            {"messages": [{"role": "system", "content": "用 system 参数"}]}, id="system 角色"
        ),
        pytest.param({"messages": [{"role": "user", "content": 123}]}, id="content 非字符串"),
        pytest.param({"messages": [{"role": "user", "content": "  "}]}, id="content 为空"),
        pytest.param({"messages": [{"role": "user"}]}, id="缺 content"),
        pytest.param({"temperature": 3}, id="temperature 超上界"),
        pytest.param({"temperature": "hot"}, id="temperature 非数字"),
    ],
)
async def test_input_contract_violations_raise_config_error(kwargs: dict) -> None:
    """发请求前先把角色、消息、response_model、temperature 校验干净，绝不带病出网。"""
    from src.plugins.ai_provider import AIConfigError

    _install(_provider())
    with pytest.raises(AIConfigError):
        await _call(**kwargs)


async def test_provider_max_tokens_floor_is_responses_protocol_limit() -> None:
    from pydantic import ValidationError

    from src.plugins.ai_provider.config import ProviderConfig

    with pytest.raises(ValidationError):
        ProviderConfig(**_provider(max_tokens=15))
    assert ProviderConfig(**_provider(max_tokens=16)).max_tokens == 16


def test_error_types_config_injection_defaults_backward_compatible() -> None:
    from src.plugins.ai_provider import AIConfigError, AIErrorTypes

    class MyConfig(Exception):
        pass

    legacy = AIErrorTypes(timeout=ValueError, auth=ValueError, service=ValueError, response=ValueError)
    assert legacy.config is AIConfigError, "旧四参数构造兼容，config 默认共享类型"
    assert AIErrorTypes(
        timeout=ValueError, auth=ValueError, service=ValueError, response=ValueError, config=MyConfig
    ).config is MyConfig


async def test_input_validation_uses_injected_config_error_type() -> None:
    class MyConfig(Exception):
        pass

    from src.plugins.ai_provider import AIErrorTypes

    _install(_provider())
    errors = AIErrorTypes(
        timeout=ValueError, auth=ValueError, service=ValueError, response=ValueError, config=MyConfig
    )
    with pytest.raises(MyConfig):
        await _call(json_object=True, response_model=_rating_model(), errors=errors)

    # response_model 非 Pydantic 模型同样走注入类型
    with pytest.raises(MyConfig, match="response_model"):
        await _call(response_model=123, errors=errors)


@pytest.mark.parametrize("status", [400, 429, 500])
async def test_error_body_message_and_request_id_are_surfaced(status: int) -> None:
    """非 2xx 时 SDK 提取 error.message 与 request-id；绝不回显请求内容。"""
    from src.plugins.ai_provider import AIServiceError

    _install(_provider())
    with (
        mock_ai_http(
            status_error(
                status,
                body={"error": {"message": "上游限流，请稍后再试", "type": "rate_limit_error"}},
                headers={"x-request-id": "req_abc123"},
            )
        ),
        pytest.raises(AIServiceError) as exc_info,
    ):
        await _call()

    message = str(exc_info.value)
    assert "上游限流，请稍后再试" in message
    assert "req_abc123" in message
    assert "sk-1" not in message, "诊断信息里不能出现 api_key"


async def test_auth_error_also_carries_error_body() -> None:
    from src.plugins.ai_provider import AIAuthError

    _install(_provider())
    with (
        mock_ai_http(status_error(401, body={"error": {"message": "Incorrect API key provided"}})),
        pytest.raises(AIAuthError) as exc_info,
    ):
        await _call()
    assert "Incorrect API key provided" in str(exc_info.value)


async def test_non_json_error_body_falls_back_to_raw_text() -> None:
    from src.plugins.ai_provider import AIServiceError

    _install(_provider())
    with (
        mock_ai_http(lambda request: httpx2.Response(502, text="<html>Bad Gateway</html>")),
        pytest.raises(AIServiceError) as exc_info,
    ):
        await _call()
    assert "Bad Gateway" in str(exc_info.value)


# ── 自适应协议 ──────────────────────────────────────────


async def test_auto_probes_candidates_in_order_and_caches() -> None:
    from src.plugins.ai_provider import auto_cached_protocol
    from src.plugins.ai_provider.config import ProviderConfig

    provider = ProviderConfig(
        **_provider(
            protocol="auto",
            base_url="https://relay.example.com/v1",
            anthropic_base_url="https://api.anthropic.com/v1",
        )
    )
    _install(
        _provider(
            protocol="auto",
            base_url="https://relay.example.com/v1",
            anthropic_base_url="https://api.anthropic.com/v1",
        )
    )
    anthropic_reply = {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "model": "gpt-test",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        if str(request.url).endswith("/chat/completions"):
            # 中转不认 Chat Completions：模拟业务字段缺失
            return httpx2.Response(200, json={})
        return httpx2.Response(200, json=anthropic_reply)

    with mock_ai_http(handler) as http:
        first = await _call()
        second = await _call()

    assert first.protocol == "anthropic_messages"
    assert second.protocol == "anthropic_messages"
    assert auto_cached_protocol(provider, "gpt-test") == "anthropic_messages"
    # 首次探测两次（chat 失败 → anthropic 成功）再发真实请求；
    # 第二次直接用缓存，只剩真实请求这一次。
    assert http.urls == [
        "https://relay.example.com/v1/chat/completions",
        "https://api.anthropic.com/v1/messages",
        "https://api.anthropic.com/v1/messages",
        "https://api.anthropic.com/v1/messages",
    ]


async def test_auto_chat_probe_omits_token_budget() -> None:
    """Chat 探测不发送预算字段：省掉 max_completion_tokens/max_tokens 的兼容性变量。"""
    _install(_provider(protocol="auto"))

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={
            "id": "chat-1",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-test",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}],
        })

    with mock_ai_http(handler) as http:
        await _call()

    probes = [body for body in http.bodies() if body.get("messages", [{}])[0].get("content") == "Reply with OK only."]
    assert len(probes) == 1
    assert probes[0]["messages"] == [{"role": "user", "content": "Reply with OK only."}]
    assert "max_completion_tokens" not in probes[0]
    assert "max_tokens" not in probes[0]


async def test_auto_cache_is_keyed_by_endpoint_model_and_credential_fingerprint() -> None:
    """端点/模型/密钥任一变化都要换指纹重新探测，不能沿用旧协议。"""
    from src.plugins.ai_provider import auto_cached_protocol
    from src.plugins.ai_provider.config import ProviderConfig, config as ai_config

    _install(_provider(protocol="auto"))

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={
            "id": "chat-1",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-test",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}],
        })

    with mock_ai_http(handler) as http:
        await _call()
        first_provider = ProviderConfig(**_provider(protocol="auto"))
        assert auto_cached_protocol(first_provider, "gpt-test") == "openai_chat"
        # 换密钥 → 新指纹 → 重新探测
        ai_config.ai_providers = [_provider(protocol="auto", api_key="sk-rotated")]
        await _call()

    assert len(http.requests) == 4, "两次完整调用各含一次探测与一次业务请求"
    assert auto_cached_protocol(
        ProviderConfig(**_provider(protocol="auto", api_key="sk-rotated")), "gpt-test"
    ) == "openai_chat"


async def test_auto_concurrent_first_probe_only_fires_once() -> None:
    """并发首次探测要被每指纹锁合并成一次。"""
    import asyncio

    _install(_provider(protocol="auto"))

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={
            "id": "chat-1",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-test",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}],
        })

    with mock_ai_http(handler) as http:
        first, second = await asyncio.gather(_call(), _call())

    assert first.protocol == second.protocol == "openai_chat"
    assert len(http.requests) == 3, "一次共享探测 + 两次业务请求"


async def test_auto_probe_failure_is_shared_and_retryable() -> None:
    """并发共享失败结果（全失败只探测一轮）；失败不缓存，后续调用可重试。"""
    import asyncio

    from src.plugins.ai_provider import AIServiceError

    _install(_provider(protocol="auto"))
    attempts = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        return httpx2.Response(500)

    with mock_ai_http(handler):
        results = await asyncio.gather(_call(), _call(), return_exceptions=True)

    assert all(isinstance(result, AIServiceError) for result in results)
    assert attempts == 3, "三种候选协议各试一次，两个等待者共享同一轮失败"

    with mock_ai_http(handler) as http:
        with pytest.raises(AIServiceError):
            await _call()
    assert len(http.requests) == 3, "失败不缓存：下一次调用重新探测"


async def test_auto_probe_failure_maps_to_each_waiters_error_types() -> None:
    """探测任务用共享层默认异常跑，等待者各自映射成注入的异常类型。"""
    import asyncio

    class MyService(Exception):
        pass

    from src.plugins.ai_provider import AIErrorTypes, AIServiceError

    _install(_provider(protocol="auto"))
    custom_errors = AIErrorTypes(
        timeout=MyService, auth=MyService, service=MyService, response=MyService
    )

    with mock_ai_http(status_error(500)):
        default_call = _call()
        custom_call = _call(errors=custom_errors)
        default_result, custom_result = await asyncio.gather(
            default_call, custom_call, return_exceptions=True
        )

    assert isinstance(default_result, AIServiceError)
    assert isinstance(custom_result, MyService), "自定义注入类型的等待者拿到自己的异常"


async def test_auto_waiter_timeout_does_not_kill_shared_probe() -> None:
    """单个等待者超时只影响自己；共享任务继续跑，后来者直接受益。"""
    import asyncio
    from unittest.mock import patch

    from src.plugins.ai_provider import AITimeoutError, service

    _install(_provider(protocol="auto"))
    real_probe = service.probe_protocol
    probe_count = 0

    async def slow_probe(**kwargs):
        nonlocal probe_count
        probe_count += 1
        await asyncio.sleep(0.5)  # 探测比第一个等待者的预算慢
        return await real_probe(**kwargs)

    with mock_ai_http(chat_ok()):
        with patch.object(service, "probe_protocol", slow_probe):
            with pytest.raises(AITimeoutError, match="探测等待超时"):
                await _call(timeout_seconds=0.2)
        result = await _call(timeout_seconds=30)

    assert result.protocol == "openai_chat"
    assert probe_count == 1, "共享任务没有被超时等待者拖垮，探测只发生一次"


async def test_auto_reset_cancels_inflight_probe_without_writeback() -> None:
    """reset 取消进行中的探测任务：等待者收到取消，缓存不被旧任务回写。"""
    import asyncio
    from unittest.mock import patch

    from src.plugins.ai_provider import auto_cached_protocol, reset_auto_cache
    from src.plugins.ai_provider import service
    from src.plugins.ai_provider.config import ProviderConfig

    _install(_provider(protocol="auto"))

    async def slow_probe(**kwargs) -> None:
        await asyncio.sleep(5)  # 足够长，reset 一定落在探测进行中

    with pytest.raises(asyncio.CancelledError):
        with patch.object(service, "probe_protocol", slow_probe):
            pending = asyncio.ensure_future(_call())
            await asyncio.sleep(0.05)  # 让探测任务真正启动并挂在 sleep 上
            reset_auto_cache()
            await pending

    provider = ProviderConfig(**_provider(protocol="auto"))
    assert auto_cached_protocol(provider, "gpt-test") is None, "取消的旧任务不能回写缓存"
    assert not service._probes


async def test_auto_responses_uses_legal_probe_budget_and_accepts_incomplete_shape() -> None:
    """Responses 至少要 16 tokens；推理耗尽后无可见文本仍是有效协议响应。"""
    from src.plugins.ai_provider import auto_cached_protocol
    from src.plugins.ai_provider.config import ProviderConfig

    provider = ProviderConfig(**_provider(protocol="auto"))
    _install(_provider(protocol="auto"))

    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        body = json.loads(request.content)
        if url.endswith("/chat/completions") or url.endswith("/messages"):
            return httpx2.Response(404)
        if body.get("max_output_tokens") == 16:
            return httpx2.Response(
                200,
                json={
                    "id": "resp-1",
                    "object": "response",
                    "created_at": 0,
                    "status": "incomplete",
                    "model": "gpt-test",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [{"type": "reasoning", "summary": []}],
                },
            )
        return httpx2.Response(
            200,
            json={
                "id": "resp-1",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "gpt-test",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "id": "m-1",
                        "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                    }
                ],
            },
        )

    with mock_ai_http(handler) as http:
        result = await _call()

    assert result.protocol == "openai_responses"
    assert result.text == "ok"
    assert auto_cached_protocol(provider, "gpt-test") == "openai_responses"
    assert [url.split("/v1/", 1)[-1] for url in http.urls] == [
        "chat/completions",
        "messages",
        "responses",
        "responses",
    ]


async def test_auto_round_waits_within_caller_budget_not_probe_cap() -> None:
    """回归：整轮探测等待用调用方预算，不被单个候选的探测超时上限截断。

    复现原缺陷的时间比例：前一个候选耗时 60% 上限后失败，后一个同样耗时后成功；
    调用方预算（5s）大于两倍候选耗时，旧实现把整轮截到单候选上限（2s）会误报超时。
    """
    import time

    from src.plugins.ai_provider import service

    _install(_provider(protocol="auto"))
    real_probe_timeout = service._PROBE_TIMEOUT_SECONDS
    service._PROBE_TIMEOUT_SECONDS = 2.0  # 单候选 HTTP 上限（缩短时间比例的本地模拟）

    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        time.sleep(1.2)  # 60% 候选上限；同步 handler 阻塞但墙钟时间照走
        if url.endswith("/chat/completions"):
            return httpx2.Response(404)
        return httpx2.Response(
            200,
            json={
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "model": "gpt-test",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    try:
        with mock_ai_http(handler):
            result = await _call(timeout_seconds=5.0)
        assert result.protocol == "anthropic_messages"
        assert result.text == "ok"

        # 反向对照：调用方预算小于首个候选的耗时仍按预算超时
        service._auto_cache.clear()
        service._probes.clear()
        from src.plugins.ai_provider import AITimeoutError

        with (
            mock_ai_http(handler),
            pytest.raises(AITimeoutError, match="探测等待超时"),
        ):
            await _call(timeout_seconds=0.2)
    finally:
        # 反向对照的超时只是放弃等待，共享探测任务可能仍在跑：清场防止串扰
        service._PROBE_TIMEOUT_SECONDS = real_probe_timeout
        service.reset_auto_cache()


async def test_auto_raises_first_error_when_every_candidate_fails() -> None:
    from src.plugins.ai_provider import AIServiceError

    _install(_provider(protocol="auto"))
    with (
        mock_ai_http(status_error(500)),
        pytest.raises(AIServiceError),
    ):
        await _call()


async def test_auto_without_any_endpoint_raises_config_error() -> None:
    from src.plugins.ai_provider import AIConfigError
    from src.plugins.ai_provider.config import config as ai_config

    ai_config.ai_providers = [_provider(protocol="auto", base_url="", api_key="sk-1")]
    with pytest.raises(AIConfigError):
        await _call()


def test_reset_auto_cache_clears_probe_result() -> None:
    from src.plugins.ai_provider import auto_cached_protocol, reset_auto_cache
    from src.plugins.ai_provider import service
    from src.plugins.ai_provider.config import ProviderConfig

    provider = ProviderConfig(**_provider(protocol="auto"))
    service._auto_cache[service._auto_fingerprint(provider, "gpt-test")] = "openai_responses"
    assert auto_cached_protocol(provider, "gpt-test") == "openai_responses"
    reset_auto_cache()
    assert auto_cached_protocol(provider, "gpt-test") is None
    assert not service._probes, "reset 也要清掉进行中的探测任务登记"


# ── 「前置插件本身没有功能」的约束 ───────────────────────


def test_plugin_registers_no_matchers() -> None:
    from nonebot import get_plugin

    plugin = get_plugin("ai_provider")

    assert plugin is not None, "conftest.load_plugins 应加载 ai_provider"
    assert plugin.matcher == set()
    assert plugin.metadata is not None
    assert plugin.metadata.name == "ai_provider"


def test_source_has_no_matcher_or_job_registration() -> None:
    """源码层面也钉住：不注册 matcher、定时任务或启动钩子。"""
    forbidden = (
        "on_regex",
        "on_command",
        "on_message",
        "on_fullmatch",
        "scheduled_job",
        "on_startup",
        "on_shutdown",
    )
    for path in _SOURCE_FILES:
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, f"{path.name} 不应出现 {token}"


def test_config_module_loads_standalone_like_web_config() -> None:
    """config.py 必须能被 web_config 的扫描器单独加载。

    扫描器用一个**不在 sys.modules 里**的合成模块名 exec 本文件（见
    ``web_config/scanner.py:_load_config_module``），所以模块级相对 import 会
    ModuleNotFoundError，模块级 ``get_plugin_config(Config)`` 会因为嵌套模型
    解析不出来而抛 PydanticUserError —— 任一情况都会让本插件的配置在管理界面里
    静默消失。这里复现同样的加载方式。
    """
    import importlib.util

    config_path = PLUGIN_ROOT / "config.py"
    spec = importlib.util.spec_from_file_location("kiana_scan_test_ai_provider", config_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # 不得抛错

    assert set(module.Config.model_fields) == {
        "ai_providers",
        "ai_default_model",
        "ai_plugin_models",
    }


def test_web_config_scanner_exposes_provider_schema() -> None:
    """扫描结果要是 list_object + 子字段，且 api_key 打上 secret。"""
    import importlib.util
    import sys

    scanner_path = PLUGIN_ROOT.parent / "web_config" / "scanner.py"
    spec = importlib.util.spec_from_file_location("kiana_scan_test_scanner", scanner_path)
    assert spec is not None and spec.loader is not None
    scanner = importlib.util.module_from_spec(spec)
    # 按文件路径加载的模块要先登记进 sys.modules：scanner 自己用了 dataclass +
    # 字符串注解，dataclasses 解析注解时找不到模块会直接报错。
    sys.modules[spec.name] = scanner
    try:
        spec.loader.exec_module(scanner)
    finally:
        sys.modules.pop(spec.name, None)

    schema = scanner.scan_plugin(PLUGIN_ROOT)
    assert schema is not None, "web_config 扫描器必须能读出 ai_provider 的配置"

    fields = {field.key: field for field in schema.fields}
    providers = fields["ai_providers"]
    assert providers.type == "list_object"
    subfields = {sub.key: sub for sub in providers.subfields or []}
    assert subfields["api_key"].secret is True
    assert subfields["protocol"].type == "enum"
    assert "anthropic_messages" in (subfields["protocol"].options or [])




async def test_sdk_http_client_is_closed_on_success_and_failure() -> None:
    """验收：成功、异常、取消路径都要关闭 SDK 传输客户端，无后台泄漏。"""
    import asyncio

    from src.plugins.ai_provider import AIAuthError

    _install(_provider())
    created: list[httpx2.AsyncClient] = []

    class RecordingMock(AIHttpMock):
        def __call__(self, timeout_seconds: float) -> httpx2.AsyncClient:
            client = super().__call__(timeout_seconds)
            created.append(client)
            return client

    # 失败路径：客户端被关闭
    with patch(AI_HTTP_TARGET, new=RecordingMock(status_error(401, body={"error": {"message": "bad key"}}))):
        with pytest.raises(AIAuthError):
            await _call()
    assert created and created[-1].is_closed, "异常路径必须关闭传输客户端"

    # 成功路径：客户端被关闭
    with patch(AI_HTTP_TARGET, new=RecordingMock(chat_ok())):
        await _call()
    assert created[-1].is_closed, "成功路径必须关闭传输客户端"

    # 取消路径：请求进行中取消任务，客户端仍被关闭
    started = asyncio.Event()

    async def slow_handler(request: httpx2.Request) -> httpx2.Response:
        started.set()
        await asyncio.sleep(1.0)  # 异步挂住请求，留出取消窗口
        return httpx2.Response(200)

    with patch(AI_HTTP_TARGET, new=RecordingMock(slow_handler)):
        task = asyncio.ensure_future(_call())
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert created[-1].is_closed, "取消路径也必须关闭客户端"
