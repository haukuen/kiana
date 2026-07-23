from nonebot import get_driver, get_plugin_config, logger
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="refine",
    description="炼化：订阅群聊发言人/集合，懒触发实时提炼 AI 总结",
    usage=(
        "炼化订阅 <标签> <user:qq | collection:名 | 集合 名 | @某人>\n"
        "炼化订阅列表\n"
        "炼化取消订阅 <标签>\n"
        "炼化 <标签>          # 缓存新鲜直接返回，过期才重炼\n"
        "强制炼化 <标签>      # 跳过新鲜检查与冷却，强制重炼\n"
        "炼化帮助"
    ),
    config=Config,
)

config: Config = get_plugin_config(Config)
driver = get_driver()


@driver.on_startup
async def _init_refine() -> None:
    """启动时仅建表 + 检查 AI 配置（缺失仅警告）。

    v2 不再注册任何 cron job —— 提炼完全由用户命令懒触发；过期清理通过
    ``refine_result`` 表与订阅 1:1 + INSERT OR REPLACE 自动处理。
    """
    from .db import ensure_schema  # noqa: PLC0415

    ensure_schema()

    missing: list[str] = []
    if config.refine_plugin_enabled:
        if not config.refine_ai_base_url.strip():
            missing.append("refine_ai_base_url")
        if not config.refine_ai_api_key.strip():
            missing.append("refine_ai_api_key")
        if not config.refine_ai_model.strip():
            missing.append("refine_ai_model")
    if missing:
        logger.warning(
            f"[refine] 配置缺失: {', '.join(missing)}。"
            "炼化命令不可用，订阅管理命令仍可用。"
        )


from . import commands  # noqa: E402  # register matchers
