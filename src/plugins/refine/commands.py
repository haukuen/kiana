"""炼化插件命令 handlers。

6 个命令:
- ``炼化订阅 <标签> <user:qq | collection:名 | 集合 名>``  新增订阅
- ``炼化订阅列表``                                          列出本群订阅
- ``炼化取消订阅 <标签>``                                   删除订阅
- ``炼化查询 [标签]``                                       拉取最新结果（懒触发）
- ``炼化刷新 <标签>``                                       立刻触发一次提炼
- ``炼化帮助``                                              帮助

权限: 所有用户可用（与 trawler 保持一致）。群权限由 group_rule 控制。

注意: ``config`` 实例从 ``__init__.py`` 复用，避免每次 ``get_plugin_config`` 新建
实例导致测试 setattr 与 group_rule 闭包取到不同实例。
"""

from __future__ import annotations

from datetime import datetime

from nonebot import on_command
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
)
from nonebot.params import CommandArg

# 复用 __init__.py 中已实例化的 config 对象，避免 get_plugin_config 每次新建实例
# 导致测试 setattr 与 group_rule 闭包取到不同实例。
import src.plugins.refine as _refine_pkg

from ..group_permission import create_group_rule
from .collector import resolve_target_type_and_value
from .config import Config
from .db import (
    RefineSubscription,
    add_subscription,
    conflict_on_label,
    conflict_on_target,
    delete_subscription,
    get_latest_result,
    get_subscription_by_label,
    list_subscriptions,
)
from .exceptions import RefineConfigError
from .runner import _validate_ai_config, refine_single_subscription

config: Config = _refine_pkg.config

# ── 命令注册 ──────────────────────────────────────────
refine_group_rule = create_group_rule(
    config_getter=lambda: config,
    plugin_enabled_attr="refine_plugin_enabled",
    prefix="refine_",
)

refine_subscribe = on_command("炼化订阅", rule=refine_group_rule, block=True)
refine_list = on_command("炼化订阅列表", rule=refine_group_rule, block=True)
refine_unsubscribe = on_command("炼化取消订阅", rule=refine_group_rule, block=True)
refine_query = on_command("炼化查询", rule=refine_group_rule, block=True)
refine_refresh = on_command("炼化刷新", rule=refine_group_rule, block=True)
refine_help = on_command("炼化帮助", rule=refine_group_rule, block=True)


# ── 工具函数 ──────────────────────────────────────────


def _extract_at_qq(message: Message) -> str | None:
    """从消息段中抽取第一个 @ 用户 QQ。"""
    for seg in message:
        if seg.type == "at" and seg.data.get("qq"):
            return str(seg.data["qq"])
    return None


def _format_subscription_line(sub: RefineSubscription) -> str:
    type_label = "用户" if sub.target_type == "user" else "集合"
    return (
        f"  • [{sub.label}] {type_label}={sub.target_value}"
        f"  (创建于 {datetime.fromtimestamp(sub.created_at).strftime('%Y-%m-%d %H:%M')})"
    )


def _format_result_block(
    sub: RefineSubscription, result, members: list[str] | None = None
) -> str:  # type: ignore[no-untyped-def]
    period_start_str = datetime.fromtimestamp(result.period_start).strftime(
        "%Y-%m-%d %H:%M"
    )
    period_end_str = datetime.fromtimestamp(result.period_end).strftime("%m-%d %H:%M")
    type_label = "用户" if sub.target_type == "user" else "集合"

    lines = [
        f"🧪 炼化结果：[{sub.label}]",
        f"目标：{type_label}={sub.target_value}",
    ]
    if members is not None and sub.target_type == "collection":
        lines.append(f"集合成员：{len(members)} 人")
    lines.extend(
        [
            f"采样窗口：{period_start_str} ~ {period_end_str}",
            f"采样消息：{result.message_count} 条",
            f"模型：{result.model_name}",
            "",
            result.summary,
        ]
    )
    return "\n".join(lines)


async def _resolve_collection_members(
    group_id: str, collection_name: str
) -> list[str]:
    """容错版本：un_nickname 未加载时返回空。"""
    try:
        from src.plugins.un_nickname.db import (  # noqa: PLC0415
            fetch_collection_members,
        )
    except ImportError:
        return []
    return await fetch_collection_members(group_id, collection_name)


# ── Handlers ─────────────────────────────────────────


@refine_subscribe.handle()
async def _subscribe(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    raw = args.extract_plain_text().strip()

    at_qq = _extract_at_qq(args)

    if at_qq:
        label = raw.split()[0] if raw.split() else ""
        target_type = "user"
        target_value = at_qq
    else:
        parts = raw.split()
        if len(parts) < 2:
            await refine_subscribe.finish(
                "用法：炼化订阅 <标签> <user:qq | collection:名 | 集合 名 | @某人>"
            )
            return
        label = parts[0]
        rest = " ".join(parts[1:])
        resolved = resolve_target_type_and_value(rest)
        if resolved[0] is None:
            await refine_subscribe.finish(
                "无法识别订阅目标。支持的写法：\n"
                "  user:<qq>\n"
                "  collection:<名>\n"
                "  集合 <名>\n"
                "  或直接 @某人"
            )
            return
        target_type = resolved[0]
        target_value = resolved[1]

    if not label:
        await refine_subscribe.finish("请指定订阅标签")

    group_id = str(event.group_id)

    if target_type == "collection":
        members = await _resolve_collection_members(group_id, target_value)
        if not members:
            await refine_subscribe.finish(
                f"集合「{target_value}」不存在或无成员。"
                "请先用 un_nickname 插件的 `集合 <名> @人` 命令创建。"
            )
            return

    dup_target = await conflict_on_target(group_id, target_type, target_value)
    if dup_target is not None:
        await refine_subscribe.finish(
            f"该目标已被订阅，标签为「{dup_target.label}」"
        )
        return
    dup_label = await conflict_on_label(group_id, label)
    if dup_label is not None:
        await refine_subscribe.finish(f"标签「{label}」已被使用，请换一个")

    sub = await add_subscription(
        group_id=group_id,
        target_type=target_type,
        target_value=target_value,
        label=label,
    )
    if sub is None:  # pragma: no cover
        await refine_subscribe.finish("订阅失败（可能存在并发冲突，请重试）")
        return

    type_label = "用户" if target_type == "user" else "集合"
    await refine_subscribe.finish(
        f"✅ 已订阅 [{label}] ({type_label}={target_value})\n"
        f"下次定时提炼：每日 {config.refine_schedule_cron_hour:02d}:"
        f"{config.refine_schedule_cron_minute:02d}\n"
        f"若需立即生成结果，发送：炼化刷新 {label}"
    )


@refine_list.handle()
async def _list(event: GroupMessageEvent) -> None:
    subs = await list_subscriptions(str(event.group_id))
    if not subs:
        await refine_list.finish("本群暂无炼化订阅。发送 `炼化订阅 <标签> @某人` 添加")
        return
    lines = [f"📌 本群共 {len(subs)} 个炼化订阅:"]
    for sub in subs:
        lines.append(_format_subscription_line(sub))
    lines.append("\n使用 `炼化查询 [标签]` 查看最新结果")
    await refine_list.finish("\n".join(lines))


@refine_unsubscribe.handle()
async def _unsubscribe(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    label = args.extract_plain_text().strip()
    if not label:
        await refine_unsubscribe.finish("用法：炼化取消订阅 <标签>")
        return
    ok = await delete_subscription(group_id=str(event.group_id), label=label)
    if ok:
        await refine_unsubscribe.finish(f"✅ 已取消订阅 [{label}]，历史结果一并删除")
    else:
        await refine_unsubscribe.finish(f"未找到标签为「{label}」的订阅")


@refine_query.handle()
async def _query(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    label = args.extract_plain_text().strip()
    group_id = str(event.group_id)

    if not label:
        subs = await list_subscriptions(group_id)
        if not subs:
            await refine_query.finish("本群暂无订阅")
            return
        lines = [f"📋 本群 {len(subs)} 个订阅的最新结果:"]
        for sub in subs:
            result = await get_latest_result(sub.id)
            if result is None:
                lines.append(f"  • [{sub.label}] 暂无结果（等待定时提炼或炼化刷新）")
            else:
                preview = result.summary.replace("\n", " ")[:60]
                ts = datetime.fromtimestamp(result.created_at).strftime(
                    "%Y-%m-%d %H:%M"
                )
                lines.append(
                    f"  • [{sub.label}] ({ts}, {result.message_count}条): {preview}..."
                )
        lines.append("\n使用 `炼化查询 <标签>` 查看完整结果")
        await refine_query.finish("\n".join(lines))
        return

    sub = await get_subscription_by_label(group_id, label)
    if sub is None:
        await refine_query.finish(f"未找到标签为「{label}」的订阅")
        return

    result = await get_latest_result(sub.id)
    if result is None:
        await refine_query.finish(
            f"订阅 [{label}] 暂无结果。发送 `炼化刷新 {label}` 立即生成"
        )
        return

    members: list[str] | None = None
    if sub.target_type == "collection":
        members = await _resolve_collection_members(group_id, sub.target_value)

    await refine_query.finish(_format_result_block(sub, result, members))


@refine_refresh.handle()
async def _refresh(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    label = args.extract_plain_text().strip()
    if not label:
        await refine_refresh.finish("用法：炼化刷新 <标签>")
        return

    group_id = str(event.group_id)
    sub = await get_subscription_by_label(group_id, label)
    if sub is None:
        await refine_refresh.finish(f"未找到标签为「{label}」的订阅")
        return

    try:
        _validate_ai_config(config)
    except RefineConfigError as e:
        await refine_refresh.finish(f"❌ AI 配置缺失：{e}")
        return

    await refine_refresh.send(f"⏳ 正在为 [{label}] 提炼，请稍候...")

    outcome = await refine_single_subscription(sub, config)
    if outcome.success:
        result = await get_latest_result(sub.id)
        if result is not None:
            members: list[str] | None = None
            if sub.target_type == "collection":
                members = await _resolve_collection_members(group_id, sub.target_value)
            await refine_refresh.finish(
                _format_result_block(sub, result, members)
            )
            return
        # pragma: no cover
        await refine_refresh.finish(
            f"✅ [{label}] 已提炼，但读回结果失败，请用 炼化查询 重试"
        )
        return

    await refine_refresh.finish(
        f"⚠️ [{label}] 提炼未完成：{outcome.error or '未知原因'}"
    )


@refine_help.handle()
async def _help() -> None:
    lines = [
        "🧪 炼化插件帮助",
        "",
        "订阅某个用户或某集合的发言，每日定时由 AI 生成简要总结；",
        "结果默认保留 3 天（可配置）。不主动推送，需手动查询。",
        "",
        "命令：",
        "  炼化订阅 <标签> user:<qq>",
        "  炼化订阅 <标签> collection:<名>",
        "  炼化订阅 <标签> 集合 <名>",
        "  炼化订阅 <标签> @某人",
        "  炼化订阅列表",
        "  炼化取消订阅 <标签>",
        "  炼化查询 [标签]      # 不带标签则列出本群所有订阅的最新摘要",
        "  炼化刷新 <标签>      # 立即重新提炼（覆盖最新结果）",
    ]
    await refine_help.finish("\n".join(lines))
