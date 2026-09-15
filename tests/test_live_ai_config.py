"""离线验证真实接口测试的配置来源，不发送网络请求。"""

from nonebot import get_driver
import pytest

from tests.test_live_ai_integration import (
    live_ai_config,
    refine_ai_config,
    sentiment_ai_config,
    word_pulse_ai_config,
)


@pytest.mark.parametrize(
    "caller, fixture, expected_model",
    [
        ("refine", refine_ai_config, "refine-model"),
        ("word_pulse", word_pulse_ai_config, "word-model"),
        ("a_share_sentiment", sentiment_ai_config, "sentiment-model"),
    ],
)
def test_live_fixture_uses_configured_route_and_restores_state(
    monkeypatch, caller, fixture, expected_model
):
    from src.plugins.ai_provider import resolve
    from src.plugins.ai_provider.config import config as ai_config

    driver_config = get_driver().config
    monkeypatch.setattr(driver_config, "ai_providers", [{
        "id": "configured",
        "protocol": "anthropic_messages",
        "base_url": "https://configured.example/v1",
        "api_key": "configured-key",
        "models": ["default-model", "refine-model", "word-model", "sentiment-model"],
        "max_tokens": 8192,
        "temperature_policy": "drop",
    }], raising=False)
    monkeypatch.setattr(driver_config, "ai_default_model", "configured/default-model", raising=False)
    monkeypatch.setattr(driver_config, "ai_plugin_models", {
        "refine": "configured/refine-model",
        "word_pulse": "configured/word-model",
        "a_share_sentiment": "configured/sentiment-model",
    }, raising=False)
    previous = ai_config.model_dump()

    # 直接执行 fixture，验证配置替换与 teardown，不执行真实接口用例。
    with monkeypatch.context() as live_patch:
        live_ai_config.__wrapped__(live_patch, None)
        fixture.__wrapped__(None)
        target = resolve(caller)
        assert target.complete
        assert target.model == expected_model
        assert target.provider.id == "configured"
        assert target.provider.protocol == "anthropic_messages"
        assert target.provider.max_tokens == 8192
        assert target.provider.temperature_policy == "drop"
        assert target.provider.api_key == "configured-key"
        assert resolve("another-plugin").model == "default-model"

    assert ai_config.model_dump() == previous


@pytest.mark.parametrize(
    "fixture", [refine_ai_config, word_pulse_ai_config, sentiment_ai_config]
)
def test_live_fixture_skips_without_real_providers(monkeypatch, fixture):
    monkeypatch.setattr(get_driver().config, "ai_providers", [], raising=False)
    live_ai_config.__wrapped__(monkeypatch, None)
    with pytest.raises(pytest.skip.Exception, match="ai_providers 未配置"):
        fixture.__wrapped__(None)
