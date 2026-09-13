"""离线验证真实接口测试的配置来源，不发送网络请求。"""

from nonebot import get_driver
import pytest

from tests.test_live_ai_integration import refine_ai_config


def test_refine_live_fixture_uses_configured_route_and_restores_state(monkeypatch):
    from src.plugins.ai_provider import resolve
    from src.plugins.ai_provider.config import config as ai_config

    driver_config = get_driver().config
    monkeypatch.setattr(driver_config, "ai_providers", [{
        "id": "configured",
        "protocol": "anthropic_messages",
        "base_url": "https://configured.example/v1",
        "api_key": "configured-key",
        "models": ["default-model", "refine-model"],
        "max_tokens": 8192,
        "temperature_policy": "drop",
    }], raising=False)
    monkeypatch.setattr(driver_config, "ai_default_model", "configured/default-model", raising=False)
    monkeypatch.setattr(driver_config, "ai_plugin_models", {"refine": "configured/refine-model"}, raising=False)
    previous = ai_config.model_dump()

    with monkeypatch.context() as live_patch:
        refine_ai_config.__wrapped__(live_patch, None)
        target = resolve("refine")
        assert target.complete
        assert target.model == "refine-model"
        assert target.provider.id == "configured"
        assert target.provider.protocol == "anthropic_messages"
        assert target.provider.max_tokens == 8192
        assert target.provider.temperature_policy == "drop"
        assert target.provider.api_key == "configured-key"
        assert resolve("another-plugin").model == "default-model"

    assert ai_config.model_dump() == previous


def test_refine_live_fixture_skips_without_real_providers(monkeypatch):
    monkeypatch.setattr(get_driver().config, "ai_providers", [], raising=False)
    with pytest.raises(pytest.skip.Exception, match="ai_providers 未配置"):
        refine_ai_config.__wrapped__(monkeypatch, None)
