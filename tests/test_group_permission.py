"""group_permission 可见性注册表测试"""

from unittest.mock import MagicMock

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent

from src.plugins.group_permission import (
    _VISIBILITY_REGISTRY,
    check_plugin_visibility,
    create_group_rule,
    create_platform_rule,
    create_sub_feature_rule,
)


@pytest.fixture(autouse=True)
def _clear_registry():
    """每个测试前后清空注册表"""
    _VISIBILITY_REGISTRY.clear()
    yield
    _VISIBILITY_REGISTRY.clear()


def _make_group_event(group_id: int = 12345) -> MagicMock:
    """创建群消息事件 mock"""
    event = MagicMock(spec=GroupMessageEvent)
    event.group_id = group_id
    return event


class TestCheckPluginVisibility:
    async def test_unregistered_plugin_returns_true(self):
        event = _make_group_event()
        assert await check_plugin_visibility("nonexistent", event) is True

    async def test_registered_plugin_returns_checker_result(self):
        event = _make_group_event()
        async def _false_checker(_):
            return False
        _VISIBILITY_REGISTRY["test"] = _false_checker
        assert await check_plugin_visibility("test", event) is False

    async def test_registered_plugin_true(self):
        event = _make_group_event()
        async def _true_checker(_):
            return True
        _VISIBILITY_REGISTRY["test"] = _true_checker
        assert await check_plugin_visibility("test", event) is True


class TestCreateGroupRuleRegistration:
    def test_registers_with_prefix(self):
        config = MagicMock()
        config.fund_plugin_enabled = True
        config.fund_group_mode = "all"
        config.fund_group_whitelist = []
        config.fund_group_blacklist = []

        create_group_rule(lambda: config, "fund_plugin_enabled", "fund_")

        assert "fund" in _VISIBILITY_REGISTRY

    def test_registers_without_prefix(self):
        config = MagicMock()
        config.my_plugin_enabled = True
        config.group_mode = "all"
        config.group_whitelist = []
        config.group_blacklist = []

        create_group_rule(lambda: config, "my_plugin_enabled")

        assert "my" in _VISIBILITY_REGISTRY

    async def test_checker_uses_group_permission_logic(self):
        config = MagicMock()
        config.fund_plugin_enabled = False
        config.fund_group_mode = "all"
        config.fund_group_whitelist = []
        config.fund_group_blacklist = []

        create_group_rule(lambda: config, "fund_plugin_enabled", "fund_")

        event = _make_group_event()
        assert await check_plugin_visibility("fund", event) is False

    async def test_checker_respects_blacklist(self):
        config = MagicMock()
        config.fund_plugin_enabled = True
        config.fund_group_mode = "blacklist"
        config.fund_group_whitelist = []
        config.fund_group_blacklist = ["12345"]

        create_group_rule(lambda: config, "fund_plugin_enabled", "fund_")

        event = _make_group_event(group_id=12345)
        assert await check_plugin_visibility("fund", event) is False

        event2 = _make_group_event(group_id=99999)
        assert await check_plugin_visibility("fund", event2) is True


class TestCreateSubFeatureRuleRegistration:
    def test_registers_with_prefix(self):
        config = MagicMock()
        config.gold_plugin_enabled = True
        config.gold_enable_price_query = True
        config.gold_group_mode = "all"
        config.gold_group_whitelist = []
        config.gold_group_blacklist = []

        create_sub_feature_rule(
            lambda: config, "gold_plugin_enabled", "gold_enable_price_query", "gold_"
        )

        assert "gold" in _VISIBILITY_REGISTRY

    async def test_checker_returns_false_when_plugin_disabled(self):
        config = MagicMock()
        config.gold_plugin_enabled = False
        config.gold_enable_price_query = True
        config.gold_group_mode = "all"
        config.gold_group_whitelist = []
        config.gold_group_blacklist = []

        create_sub_feature_rule(
            lambda: config, "gold_plugin_enabled", "gold_enable_price_query", "gold_"
        )

        event = _make_group_event()
        assert await check_plugin_visibility("gold", event) is False

    async def test_checker_returns_false_when_feature_disabled(self):
        config = MagicMock()
        config.gold_plugin_enabled = True
        config.gold_enable_price_query = False
        config.gold_group_mode = "all"
        config.gold_group_whitelist = []
        config.gold_group_blacklist = []

        create_sub_feature_rule(
            lambda: config, "gold_plugin_enabled", "gold_enable_price_query", "gold_"
        )

        event = _make_group_event()
        assert await check_plugin_visibility("gold", event) is False


class TestCreatePlatformRuleRegistration:
    def test_registers_platform(self):
        config = MagicMock()
        config.enable_bilibili = True
        config.bilibili_group_mode = "all"
        config.bilibili_group_whitelist = []
        config.bilibili_group_blacklist = []

        create_platform_rule(lambda: config, "bilibili")

        assert "bilibili" in _VISIBILITY_REGISTRY

    async def test_checker_respects_platform_disabled(self):
        config = MagicMock()
        config.enable_bilibili = False
        config.bilibili_group_mode = "all"
        config.bilibili_group_whitelist = []
        config.bilibili_group_blacklist = []

        create_platform_rule(lambda: config, "bilibili")

        event = _make_group_event()
        assert await check_plugin_visibility("bilibili", event) is False
