"""词频通过真实 SDK 验证共享路由、三协议严格结构化输出与错误绑定。"""

import json

import httpx2
import pytest

from tests.ai_mock_transport import mock_ai_http


def _reply(protocol, content, failure=None):
    if protocol == "openai_chat":
        return {
            "choices": [{
                "finish_reason": "length" if failure == "truncated" else "stop",
                "message": {
                    "role": "assistant", "content": content,
                    "refusal": "拒绝" if failure == "refused" else None,
                },
            }],
        }
    if protocol == "anthropic_messages":
        return {
            "content": [{"type": "text", "text": content}],
            "stop_reason": {"truncated": "max_tokens", "refused": "refusal"}.get(
                failure, "end_turn"
            ),
        }
    return {
        "status": "incomplete" if failure == "truncated" else "completed",
        "incomplete_details": (
            {"reason": "max_output_tokens"} if failure == "truncated" else None
        ),
        "output": [{
            "type": "message", "id": "msg-1", "role": "assistant",
            "status": "completed",
            "content": (
                [{"type": "refusal", "refusal": "拒绝"}]
                if failure == "refused"
                else [{"type": "output_text", "text": content, "annotations": []}]
            ),
        }],
    }


def _install(protocol):
    from src.plugins.ai_provider.config import config

    config.ai_providers = [*config.ai_providers, {
        "id": "word", "protocol": protocol,
        "base_url": "https://word.example/v1", "api_key": "word-key",
        "models": ["word-model"], "max_tokens": 2048, "temperature_policy": "send",
    }]
    config.ai_plugin_models = {"word_pulse": "word/word-model"}


def _wire_schema(protocol: str, body: dict) -> dict:
    if protocol == "openai_chat":
        contract = body["response_format"]["json_schema"]
        assert contract["strict"] is True
        return contract["schema"]
    if protocol == "openai_responses":
        contract = body["text"]["format"]
        assert contract["strict"] is True
        return contract["schema"]
    return body["output_config"]["format"]["schema"]


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic_messages"])
@pytest.mark.parametrize("operation", ["expand", "classify", "summarize"])
async def test_word_pulse_operations_use_shared_route_and_strict_schema(protocol, operation):
    from src.plugins.word_pulse.ai import classify_batch, expand_charsets, summarize

    _install(protocol)
    payloads = {
        "expand": {"charsets": [{"cluster": "茅台", "chars": ["茅", "台", "酒", "业", "股"]}]},
        "classify": {"results": [{"id": 1, "cluster": "茅台"}]},
        "summarize": {
            "ranking": [], "trend": "讨论增加", "examples": [],
            "unclassified_high_freq": [],
        },
    }
    content = json.dumps(payloads[operation], ensure_ascii=False)
    with mock_ai_http(
        lambda request: httpx2.Response(200, json=_reply(protocol, content))
    ) as http:
        if operation == "expand":
            result = await expand_charsets(seeds=["茅台"], theme="炒股")
            assert result == {"茅台": ["茅", "台", "酒", "业", "股"]}
        elif operation == "classify":
            result = await classify_batch(
                messages=[(1, "茅子涨了")], clusters=[{"name": "茅台"}], theme_name="炒股"
            )
            assert result == [(1, "茅台")]
        else:
            result = await summarize(prompt="炒股统计")
            assert result.trend == "讨论增加"

    assert len(http.requests) == 1
    body = http.bodies()[0]
    assert body["model"] == "word-model"
    request = http.requests[0]
    assert request.url.host == "word.example"
    schema = _wire_schema(protocol, body)
    assert schema["additionalProperties"] is False
    assert schema["required"]
    if protocol == "anthropic_messages":
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "word-key"
        assert body["max_tokens"] == 2048
        assert "temperature" not in body
    else:
        assert request.headers["authorization"] == "Bearer word-key"
        assert body["store"] is False
        assert body["temperature"] == (0.3 if operation == "summarize" else 0.0)
        if protocol == "openai_chat":
            assert request.url.path == "/v1/chat/completions"
            assert body["max_completion_tokens"] == 2048
        else:
            assert request.url.path == "/v1/responses"
            assert body["max_output_tokens"] == 2048


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses", "anthropic_messages"])
@pytest.mark.parametrize("failure", ["truncated", "refused"])
async def test_word_pulse_rejects_incomplete_or_refused_output(protocol, failure):
    from src.plugins.word_pulse.ai import (
        BatchClassificationResponse,
        WordPulseAIResponseError,
        _request_llm,
    )

    _install(protocol)
    with (
        mock_ai_http(
            lambda request: httpx2.Response(
                200, json=_reply(protocol, '{"results": []}', failure)
            )
        ),
        pytest.raises(WordPulseAIResponseError),
    ):
        await _request_llm(
            response_model=BatchClassificationResponse,
            messages=[{"role": "user", "content": "测试"}],
            temperature=0,
            timeout_seconds=10,
        )

async def test_word_pulse_missing_route_is_plugin_config_error_without_request():
    from src.plugins.ai_provider.config import config
    from src.plugins.word_pulse.ai import WordPulseAIConfigError, expand_charsets

    config.ai_plugin_models = {"word_pulse": "missing/model"}
    with (
        mock_ai_http(lambda request: pytest.fail("配置错误不应发送请求")) as http,
        pytest.raises(WordPulseAIConfigError, match="missing"),
    ):
        await expand_charsets(seeds=["茅台"], theme="炒股")
    assert not http.requests


def test_word_pulse_config_no_longer_owns_endpoint():
    from src.plugins.word_pulse.config import Config

    assert not {
        "word_pulse_base_url", "word_pulse_api_key", "word_pulse_model",
    } & Config.model_fields.keys()
