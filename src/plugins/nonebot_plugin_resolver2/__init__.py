from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata

from .config import (
    Config,
    plugin_cache_dir,
    rconfig,
    scheduler,
    ytb_cookies_file,
)
from .cookie import save_cookies_to_netscape
from .matchers import resolvers

__plugin_meta__ = PluginMetadata(
    name="链接分享自动解析",
    description="在原版基础移除不需要的解析器和文字输出",
    usage="发送支持平台的(BV号/链接/小程序/卡片)即可",
    type="application",
    homepage="https://github.com/fllesser/nonebot-plugin-resolver2",
    config=Config,
    supported_adapters={"~onebot.v11"},
    extra={
        "author": "fllesser",
        "email": "fllessive@gmail.com",
        "homepage": "https://github.com/fllesser/nonebot-plugin-resolver2",
    },
)


@get_driver().on_startup
async def _():
    destroy_resolvers: list[str] = []

    # 关闭全局禁用的解析
    for resolver in rconfig.r_disable_resolvers:
        if matcher := resolvers.get(resolver, None):
            matcher.destroy()
            destroy_resolvers.append(resolver)
    if destroy_resolvers:
        logger.warning(f"已关闭解析: {', '.join(destroy_resolvers)}")


@scheduler.scheduled_job("cron", hour=1, minute=0, id="resolver2-clean-local-cache")
async def clean_plugin_cache():
    import asyncio

    from .download.utils import safe_unlink

    try:
        files = [f for f in plugin_cache_dir.iterdir() if f.is_file()]
        if not files:
            logger.info("no cache files to clean")
            return

        # 并发删除文件
        tasks = [safe_unlink(file) for file in files]
        await asyncio.gather(*tasks)

        logger.info(f"Successfully cleaned {len(files)} cache files")
    except Exception as e:
        logger.error(f"Error while cleaning cache: {e}")
