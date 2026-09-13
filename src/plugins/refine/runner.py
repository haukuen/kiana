"""炼化执行核心：采集 → AI → 落库。

v2 — 懒重炼：不再有定时任务遍历所有订阅；只在用户调用 `炼化` / `强制炼化`
命令时被 commands 层调用本模块的 ``refine_subscription``。

设计要点:
- AI 配置缺失 → 抛 ``RefineConfigError``，commands 层给中文提示。
- 窗口内消息不足 → 返回 ``RefineOutcome``，``success=False``，不抛。
- AI 调用失败 → 抛 ``RefineAIError`` 子类，commands 层决定回退到旧缓存。
- 落库用 ``save_result``（INSERT OR REPLACE），自动覆盖旧记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nonebot import logger, require

from .ai import CALLER, request_refine_summary
from .collector import collect_and_build_payload
from .db import RefineSubscription, save_result
from .exceptions import RefineConfigError

_ai = require("src.plugins.ai_provider")

if TYPE_CHECKING:
    from src.plugins.ai_provider import Target

    from .config import Config


@dataclass(slots=True)
class RefineOutcome:
    """单次提炼结果摘要。

    ``success=True`` 时 ``result`` 字段为新生成的 ``RefineResult``。
    ``success=False`` 时 ``reason`` 描述失败/跳过原因（"消息不足" / "AI 失败: xxx"）。
    """

    success: bool
    reason: str | None = None


def missing_ai_config() -> tuple[str, ...]:
    """返回 AI 配置的问题描述（空元组表示配置齐全）。

    端点与模型由 ai_provider 前置插件按 ``ai_plugin_models["refine"]`` 解析，
    用于启动时的告警。
    """
    return _ai.resolve(CALLER).missing


def validate_ai_config() -> Target:
    """AI 配置不齐时抛 ``RefineConfigError``，否则返回解析好的调用目标。

    端点由 ai_provider 前置插件持有，因此这里不再接收本插件的 Config。
    """
    target = _ai.resolve(CALLER)
    if target.missing:
        raise RefineConfigError(target.missing[0])
    return target


async def refine_subscription(
    sub: RefineSubscription,
    config: Config,
) -> RefineOutcome:
    """对一个订阅跑一次采集 + AI + 落库。

    Returns:
        RefineOutcome: success=True 表示已落库；success=False 表示跳过（消息不足
        或 AI 失败）。AI 失败时抛 RefineAIError（commands 层决定回退到旧缓存）。

    Raises:
        RefineConfigError: AI 配置缺失或模型解析不出来。
        RefineAIError: AI 调用失败（含子类 RefineAITimeoutError 等）。
    """
    target = validate_ai_config()

    collected, payload = await collect_and_build_payload(sub, config)

    if len(collected.messages) < config.refine_min_messages_to_refine:
        logger.info(
            f"[炼化] 订阅 {sub.label} (group={sub.group_id}) "
            f"窗口内仅 {len(collected.messages)} 条消息，不足 "
            f"{config.refine_min_messages_to_refine}，跳过"
        )
        return RefineOutcome(success=False, reason="消息不足，跳过")

    summary = await request_refine_summary(
        timeout_seconds=config.refine_ai_timeout_seconds,
        temperature=config.refine_ai_temperature,
        prompt_payload=payload,
    )

    await save_result(
        subscription_id=sub.id,
        period_start=collected.period_start,
        period_end=collected.period_end,
        summary=summary,
        message_count=len(collected.messages),
        model_name=target.model,
    )
    logger.info(f"[炼化] 订阅 {sub.label} 提炼成功 ({len(collected.messages)} 条消息)")
    return RefineOutcome(success=True)
