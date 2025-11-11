import pytest
from nonebug import App
from nonebot import init
import pytest_asyncio


@pytest_asyncio.fixture
async def app():
    init()
    return App()


@pytest_asyncio.fixture
async def fund_plugin(app: App):
    from nonebot import get_plugin
    plugin = get_plugin("fund")
    if plugin is None:
        from nonebot import load_plugin
        load_plugin("src.plugins.fund")
        plugin = get_plugin("fund")
    return plugin


@pytest.fixture
def fund_plugin():
    from nonebot import get_plugin
    plugin = get_plugin("fund")
    if plugin is None:
        pytest.skip("fund 插件未加载")
    return plugin