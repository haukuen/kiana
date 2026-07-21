"""炼化调度核心：周期遍历订阅 → 采集 → AI 提炼 → 落库；以及过期结果清理。

设计要点:
- 调度器 **只写库不推送**。用户必须用 `炼化查询` 命令手动拉结果（需求明确）。
- 单次失败只 warning 不抛，不影响其他订阅。
- ``refine_subscription`` 引用的 un_nickname 集合可能已被删（成员空），此时
  采集为空 → 跳过 AI 调用，记录 info 而非失败。
- 过期清理独立 cron，每日凌晨执行。

文件命名为 ``runner.py``（非 ``scheduler.py``）以避免与 apscheduler 注入的
``scheduler`` 变量在 pyright 中产生命名空间歧义。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nonebot import logger

from .ai import request_refine_summary
from .collector import collect_and_build_payload
from .db import (
    RefineSubscription,
    add_result,
    list_all_subscriptions,
    purge_expired_results,
)
from .exceptions import RefineAIError, RefineConfigError

if TYPE_CHECKING:
    from .config import Config


@dataclass(slots=True)
class RefineRunOutcome:
    """单次提炼结果摘要（用于日志与即时刷新命令回执）。"""

    subscription_id: int
    success: bool
    message_count: int
    error: str | None = None
    summary_preview: str | None = None


def _validate_ai_config(config: Config) -> None:
    if not config.refine_ai_base_url.strip():
        raise RefineConfigError("refine_ai_base_url 未配置")
    if not config.refine_ai_api_key.strip():
        raise RefineConfigError("refine_ai_api_key 未配置")
    if not config.refine_ai_model.strip():
        raise RefineConfigError("refine_ai_model 未配置")


async def refine_single_subscription(
    sub: RefineSubscription,
    config: Config,
) -> RefineRunOutcome:
    """对单个订阅跑一次采集 + AI + 落库。"""
    try:
        _validate_ai_config(config)
    except RefineConfigError as e:
        return RefineRunOutcome(
            subscription_id=sub.id, success=False, message_count=0, error=str(e)
        )

    collected, payload = await collect_and_build_payload(sub, config)

    if len(collected.messages) < config.refine_min_messages_to_refine:
        logger.info(
            f"[炼化] 订阅 {sub.label} (group={sub.group_id}) "
            f"窗口内仅 {len(collected.messages)} 条消息，不足 "
            f"{config.refine_min_messages_to_refine}，跳过"
        )
        return RefineRunOutcome(
            subscription_id=sub.id,
            success=False,
            message_count=len(collected.messages),
            error="消息不足，跳过",
        )

    try:
        summary = await request_refine_summary(
            base_url=config.refine_ai_base_url.strip(),
            api_key=config.refine_ai_api_key.strip(),
            model=config.refine_ai_model.strip(),
            timeout_seconds=config.refine_ai_timeout_seconds,
            temperature=config.refine_ai_temperature,
            prompt_payload=payload,
        )
    except RefineAIError as e:
        logger.warning(f"[炼化] 订阅 {sub.label} AI 失败: {e}")
        return RefineRunOutcome(
            subscription_id=sub.id,
            success=False,
            message_count=len(collected.messages),
            error=str(e),
        )

    await add_result(
        subscription_id=sub.id,
        period_start=collected.period_start,
        period_end=collected.period_end,
        summary=summary,
        message_count=len(collected.messages),
        model_name=config.refine_ai_model.strip(),
    )
    logger.info(
        f"[炼化] 订阅 {sub.label} 提炼成功 ({len(collected.messages)} 条消息)"
    )
    return RefineRunOutcome(
        subscription_id=sub.id,
        success=True,
        message_count=len(collected.messages),
        summary_preview=summary[:80],
    )


async def run_daily_refine(config: Config) -> list[RefineRunOutcome]:
    """遍历全部订阅跑一次。供 cron 与 `炼化刷新` 命令共用。"""
    subs = await list_all_subscriptions()
    outcomes: list[RefineRunOutcome] = []
    for sub in subs:
        try:
            outcome = await refine_single_subscription(sub, config)
        except Exception as e:
            logger.exception(f"[炼化] 订阅 {sub.label} 异常")
            outcomes.append(
                RefineRunOutcome(
                    subscription_id=sub.id,
                    success=False,
                    message_count=0,
                    error=f"未知异常: {e}",
                )
            )
            continue
        outcomes.append(outcome)
    return outcomes


async def run_purge(config: Config) -> int:
    """清理过期结果。返回清理条数。"""
    deleted = await purge_expired_results(config.refine_result_retention_days)
    if deleted:
        logger.info(
            f"[炼化] 清理 {deleted} 条过期结果 "
            f"(> {config.refine_result_retention_days} 天)"
        )
    return deleted
