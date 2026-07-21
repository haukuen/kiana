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

from nonebot import logger

from .ai import request_refine_summary
from .collector import collect_and_build_payload
from .db import RefineSubscription, save_result
from .exceptions import RefineConfigError

if TYPE_CHECKING:
    from .config import Config


@dataclass(slots=True)
class RefineOutcome:
    """单次提炼结果摘要。

    ``success=True`` 时 ``result`` 字段为新生成的 ``RefineResult``。
    ``success=False`` 时 ``reason`` 描述失败/跳过原因（"消息不足" / "AI 失败: xxx"）。
    """

    success: bool
    reason: str | None = None


def validate_ai_config(config: Config) -> None:
    """AI 配置缺失时抛 ``RefineConfigError``。"""
    if not config.refine_ai_base_url.strip():
        raise RefineConfigError("refine_ai_base_url 未配置")
    if not config.refine_ai_api_key.strip():
        raise RefineConfigError("refine_ai_api_key 未配置")
    if not config.refine_ai_model.strip():
        raise RefineConfigError("refine_ai_model 未配置")


async def refine_subscription(
    sub: RefineSubscription,
    config: Config,
) -> RefineOutcome:
    """对一个订阅跑一次采集 + AI + 落库。

    Returns:
        RefineOutcome: success=True 表示已落库；success=False 表示跳过（消息不足
        或 AI 失败）。AI 失败时抛 RefineAIError（commands 层决定回退到旧缓存）。

    Raises:
        RefineConfigError: AI 配置缺失。
        RefineAIError: AI 调用失败（含子类 RefineAITimeoutError 等）。
    """
    validate_ai_config(config)

    collected, payload = await collect_and_build_payload(sub, config)

    if len(collected.messages) < config.refine_min_messages_to_refine:
        logger.info(
            f"[炼化] 订阅 {sub.label} (group={sub.group_id}) "
            f"窗口内仅 {len(collected.messages)} 条消息，不足 "
            f"{config.refine_min_messages_to_refine}，跳过"
        )
        return RefineOutcome(success=False, reason="消息不足，跳过")

    summary = await request_refine_summary(
        base_url=config.refine_ai_base_url.strip(),
        api_key=config.refine_ai_api_key.strip(),
        model=config.refine_ai_model.strip(),
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
        model_name=config.refine_ai_model.strip(),
    )
    logger.info(
        f"[炼化] 订阅 {sub.label} 提炼成功 ({len(collected.messages)} 条消息)"
    )
    return RefineOutcome(success=True)
