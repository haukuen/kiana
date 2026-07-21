from nonebot import get_driver, get_plugin_config, logger, require
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="refine",
    description="炼化：订阅群聊发言人/集合，周期性 AI 提炼总结，懒触发查询",
    usage=(
        "炼化订阅 <标签> <user:qq | collection:名 | 集合 名 | @某人>\n"
        "炼化订阅列表\n"
        "炼化取消订阅 <标签>\n"
        "炼化查询 [标签]    # 不带标签则列出本群所有订阅的最新摘要\n"
        "炼化刷新 <标签>    # 立即重新提炼（覆盖最新结果）\n"
        "炼化帮助"
    ),
    config=Config,
)

config: Config = get_plugin_config(Config)
driver = get_driver()
aps = require("nonebot_plugin_apscheduler")
aps_scheduler = aps.scheduler


def _collect_missing_ai_config() -> list[str]:
    """若启用插件但缺 AI 配置，返回缺失字段名列表；否则返回空。"""
    if not config.refine_plugin_enabled:
        return []
    missing: list[str] = []
    if not config.refine_ai_base_url.strip():
        missing.append("refine_ai_base_url")
    if not config.refine_ai_api_key.strip():
        missing.append("refine_ai_api_key")
    if not config.refine_ai_model.strip():
        missing.append("refine_ai_model")
    return missing


@driver.on_startup
async def _init_refine() -> None:
    """启动时：建表 + 注册 cron（条件性）。

    缺 AI 配置仅警告，不阻止加载（订阅命令仍可用，刷新会给出明确错误）。
    插件未启用时直接跳过 cron 注册，避免空跑。
    """
    from .db import ensure_schema  # noqa: PLC0415

    ensure_schema()

    missing = _collect_missing_ai_config()
    if missing:
        logger.warning(
            f"[refine] 配置缺失: {', '.join(missing)}。"
            "定时提炼与即时刷新不可用，订阅管理命令仍可用。"
        )

    if not config.refine_plugin_enabled:
        logger.info("[refine] 插件未启用，跳过注册 cron")
        return

    from .runner import run_daily_refine, run_purge  # noqa: PLC0415

    @aps_scheduler.scheduled_job(
        "cron",
        hour=config.refine_schedule_cron_hour,
        minute=config.refine_schedule_cron_minute,
    )
    async def _daily_refine_job() -> None:
        try:
            outcomes = await run_daily_refine(config)
            ok = sum(1 for o in outcomes if o.success)
            skipped = sum(1 for o in outcomes if not o.success)
            logger.info(
                f"[refine] 每日提炼完成: 成功 {ok} 个，跳过/失败 {skipped} 个"
            )
        except Exception as e:
            logger.exception(f"[refine] 每日提炼任务异常: {e}")

    @aps_scheduler.scheduled_job("cron", hour=4, minute=30)
    async def _daily_purge_job() -> None:
        try:
            await run_purge(config)
        except Exception as e:
            logger.exception(f"[refine] 清理任务异常: {e}")


from . import commands  # noqa: E402  # register matchers
