import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from nonebug import NONEBOT_INIT_KWARGS

TEST_DB_PATH = Path(tempfile.gettempdir()) / "kiana-pytest.sqlite3"


def pytest_configure(config: pytest.Config) -> None:
    """配置 NoneBot 初始化参数"""
    os.environ["KIANA_DB_PATH"] = str(TEST_DB_PATH)
    for suffix in ("", "-shm", "-wal"):
        Path(f"{TEST_DB_PATH}{suffix}").unlink(missing_ok=True)

    config.stash[NONEBOT_INIT_KWARGS] = {
        "driver": "~fastapi",
    }


@pytest.fixture(scope="session", autouse=True)
async def load_plugins(_nonebot_init: None):
    """在 NoneBot 初始化后自动加载插件"""
    from nonebot import load_plugin

    load_plugin("src.plugins.fund")
    load_plugin("src.plugins.gold")
    load_plugin("src.plugins.message_archive")
    load_plugin("src.plugins.chat_forward")
    load_plugin("src.plugins.a_share_sentiment")
    load_plugin("src.plugins.un_nickname")
    load_plugin("src.plugins.refine")
    load_plugin("src.plugins.word_pulse")

    from src.plugins.message_archive.db import ensure_schema
    from src.plugins.refine.db import ensure_schema as ensure_refine_schema
    from src.plugins.un_nickname.db import (
        ensure_schema as ensure_un_nickname_schema,
    )

    ensure_schema()
    ensure_refine_schema()
    ensure_un_nickname_schema()


@pytest.fixture(autouse=True)
def reset_global_mute_cache() -> None:
    """每个用例前重置全局禁言缓存，避免用例间状态污染。"""
    from src import plugins as global_plugins

    global_plugins._mute_cache.clear()


@pytest.fixture(autouse=True)
def reset_chat_forward_cooldown() -> None:
    """每个用例前重置打包消息冷却状态。"""
    from src.plugins.chat_forward import cooldown_dict

    cooldown_dict.clear()


@pytest.fixture(autouse=True)
def reset_a_share_sentiment_state() -> None:
    """每个用例前重置 A 股情绪插件状态。"""
    from src.plugins.a_share_sentiment import cooldown_dict, result_cache

    cooldown_dict.clear()
    result_cache.clear()


@pytest_asyncio.fixture(autouse=True)
async def reset_message_archive_table() -> None:
    """每个用例前清空消息归档表。"""
    from src.plugins.message_archive.db import ensure_schema
    from src.plugins.refine.db import ensure_schema as ensure_refine_schema
    from src.plugins.un_nickname.db import (
        ensure_schema as ensure_un_nickname_schema,
    )
    from src.storage import get_db

    ensure_schema()
    ensure_refine_schema()
    ensure_un_nickname_schema()
    await get_db().execute("DELETE FROM message_archive")
    await get_db().execute("DELETE FROM message_archive_image")
    await get_db().execute("DELETE FROM refine_result")
    await get_db().execute("DELETE FROM refine_subscription")
    await get_db().execute("DELETE FROM nicknames")
    await get_db().execute("DELETE FROM nickname_collections")


@pytest_asyncio.fixture(autouse=True)
async def reset_word_pulse_tables() -> None:
    """每个用例前清空 word_pulse 表。"""
    from src.plugins.word_pulse.db import ensure_schema
    from src.storage import get_db

    ensure_schema()
    await get_db().execute("DELETE FROM word_pulse_bucket")
    await get_db().execute("DELETE FROM word_pulse_charset")
    await get_db().execute("DELETE FROM word_pulse_cluster")
    await get_db().execute("DELETE FROM word_pulse_theme")


@pytest_asyncio.fixture
async def fund_plugin():
    """获取 fund 插件实例"""
    from nonebot import get_plugin

    plugin = get_plugin("fund")
    if plugin is None:
        pytest.skip("fund 插件未加载")

    return plugin
