"""炼化插件命令 handlers。

6 个命令（v2 — 懒触发实时提炼）:
- ``炼化订阅 <标签> <user:qq | collection:名 | 集合 名 | @某人>``  新增订阅
- ``炼化订阅列表``                                              列出本群订阅
- ``炼化取消订阅 <标签>``                                       删除订阅
- ``炼化 <标签>``                                               懒触发：缓存新鲜直接返回，过期才重炼
- ``强制炼化 <标签>``                                           跳过新鲜检查与冷却，强制重炼
- ``炼化帮助``                                                  帮助

权限: 所有用户可用（与 trawler 保持一致）。群权限由 group_rule 控制。

工作机制:
- ``炼化`` 命令查 ``refine_result`` 表：若结果在新鲜期内（``refine_result_fresh_seconds``）
  直接返回缓存；否则实时提炼并落库。
- 提炼冷却（``refine_query_cooldown_seconds``）内重复调用直接返回旧缓存，防止高频查询
  撑爆 AI 账单。**不警告** —— 用户合理重复查询。
- ``强制炼化`` 跳过新鲜检查与冷却，每次都重炼。

注意: ``config`` 实例从 ``__init__.py`` 复用，避免每次 ``get_plugin_config`` 新建
实例导致测试 setattr 与 group_rule 闭包取到不同实例。
"""

from __future__ import annotations

import time
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
    get_result,
    get_subscription_by_label,
    list_subscriptions,
)
from .exceptions import RefineAIError, RefineConfigError
from .runner import refine_subscription, validate_ai_config

config: Config = _refine_pkg.config

# ── 冷却 dict ──────────────────────────────────────────
# key: (group_id, label)，value: 上次重炼成功的 time.time() 时间戳。
# 冷却期内 `炼化` 命令直接返回旧缓存；`强制炼化` 命令忽略此 dict。
cooldown_dict: dict[tuple[str, str], float] = {}

# ── 命令注册 ──────────────────────────────────────────
# 注意：`强制炼化` 必须先于 `炼化` 注册，避免 NoneBot 命令匹配歧义
# （虽然 on_command 是按完整命令名匹配，但保持显式顺序更安全）。
refine_group_rule = create_group_rule(
    config_getter=lambda: config,
    plugin_enabled_attr="refine_plugin_enabled",
    prefix="refine_",
)

# force_whitespace=True: 命令字后必须有空格（或无参数）才触发，防止
# `.env.prod` 的 COMMAND_START=["/", ""]（含空串）让 `炼化这个功能怎么用`
# 这样的粘连文本被误识别为 `炼化` 命令。bug#1 修复。
refine_subscribe = on_command(
    "炼化订阅", rule=refine_group_rule, force_whitespace=True, block=True
)
refine_list = on_command(
    "炼化订阅列表", rule=refine_group_rule, force_whitespace=True, block=True
)
refine_unsubscribe = on_command(
    "炼化取消订阅", rule=refine_group_rule, force_whitespace=True, block=True
)
# 强制炼化先注册
refine_force = on_command(
    "强制炼化", rule=refine_group_rule, force_whitespace=True, block=True
)
refine_lazy = on_command(
    "炼化", rule=refine_group_rule, force_whitespace=True, block=True
)
refine_help = on_command(
    "炼化帮助", rule=refine_group_rule, force_whitespace=True, block=True
)


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


def _format_old_result_age(created_at: int) -> str:
    """把旧结果的 created_at 格式化为「X 小时前」/「X 分钟前」字符串。"""
    delta = max(0, int(time.time()) - created_at)
    hours = delta // 3600
    if hours >= 1:
        return f"{hours} 小时前"
    minutes = max(1, delta // 60)
    return f"{minutes} 分钟前"


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


async def _resolve_members_for_sub(
    sub: RefineSubscription, group_id: str
) -> list[str] | None:
    """如果是 collection 订阅，返回成员列表；否则返回 None。"""
    if sub.target_type == "collection":
        return await _resolve_collection_members(group_id, sub.target_value)
    return None


async def _return_cached_or_old_result(
    matcher,
    sub: RefineSubscription,
    group_id: str,
    result,
    *,
    prefix: str = "",
) -> None:  # type: ignore[no-untyped-def]
    """格式化并返回（缓存的或旧的）结果，可带警告前缀。"""
    members = await _resolve_members_for_sub(sub, group_id)
    block = _format_result_block(sub, result, members)
    if prefix:
        age = _format_old_result_age(result.created_at)
        await matcher.finish(f"{prefix}（{age}）：\n\n{block}")
    else:
        await matcher.finish(block)


async def _run_refine_and_reply(
    matcher,
    sub: RefineSubscription,
    group_id: str,
    label: str,
    old_result,
    *,
    allow_old_fallback_on_insufficient: bool,
) -> None:  # type: ignore[no-untyped-def]
    """执行 refine_subscription 并格式化输出。

    - AI 异常：有旧结果→返回带警告的旧缓存；无旧结果→返回 ❌ 提炼失败。
    - outcome.success=False：
        * allow_old_fallback_on_insufficient=True 时（炼化 命令），
          有旧结果→返回带警告的旧缓存；无旧结果→提示发言不足。
        * False 时（强制炼化 命令）直接提示发言不足。
    - success=True：更新冷却 dict，读回新结果，返回新结果。
    """
    try:
        outcome = await refine_subscription(sub, config)
    except RefineAIError as e:
        if old_result is not None:
            await _return_cached_or_old_result(
                matcher, sub, group_id, old_result,
                prefix="⚠️ AI 调用失败，显示上次结果",
            )
            return
        await matcher.finish(f"❌ 提炼失败：{e}")
        return
    except RefineConfigError as e:  # pragma: no cover
        await matcher.finish(f"❌ 配置错误：{e}")
        return

    if not outcome.success:
        if allow_old_fallback_on_insufficient and old_result is not None:
            await _return_cached_or_old_result(
                matcher, sub, group_id, old_result,
                prefix="⚠️ 目标近期发言不足，显示上次结果",
            )
            return
        await matcher.finish("⚠️ 目标近期发言不足，请等目标多说话后重试")
        return

    cooldown_dict[(group_id, label)] = time.time()
    new_result = await get_result(sub.id)
    if new_result is None:  # pragma: no cover
        await matcher.finish(
            f"✅ [{label}] 已提炼，但读回结果失败，请用 炼化 {label} 重试"
        )
        return
    await _return_cached_or_old_result(matcher, sub, group_id, new_result)


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
        f"发送 `炼化 {label}` 查看结果（首次查询会触发提炼）"
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
    lines.append("\n使用 `炼化 <标签>` 查看或触发提炼")
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


@refine_lazy.handle()
async def _lazy(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    """懒触发炼化：缓存新鲜直接返回；过期且不在冷却内才实时提炼。

    - 新鲜期内 → 直接返回缓存
    - 冷却期内（缓存不新鲜）→ 静默返回旧缓存（用户合理重复查询，不警告）
    - 否则 → 实时提炼并落库
    """
    label = args.extract_plain_text().strip()
    if not label:
        await refine_lazy.finish("用法：炼化 <标签>")
        return

    group_id = str(event.group_id)
    sub = await get_subscription_by_label(group_id, label)
    if sub is None:
        await refine_lazy.finish(f"未找到标签为「{label}」的订阅")
        return

    try:
        validate_ai_config(config)
    except RefineConfigError as e:
        await refine_lazy.finish(f"❌ AI 配置缺失：{e}")
        return

    result = await get_result(sub.id)
    now = time.time()

    # 新鲜期内 → 直接返回缓存（不调 AI）
    if result is not None and now - result.created_at < config.refine_result_fresh_seconds:
        await _return_cached_or_old_result(refine_lazy, sub, group_id, result)
        return

    # 冷却期内 → 静默返回旧缓存（用户合理重复查询）
    last_refine_ts = cooldown_dict.get((group_id, label))
    if (
        result is not None
        and last_refine_ts is not None
        and now - last_refine_ts < config.refine_query_cooldown_seconds
    ):
        await _return_cached_or_old_result(refine_lazy, sub, group_id, result)
        return

    await refine_lazy.send(f"⏳ 正在为 [{label}] 提炼，请稍候...")

    await _run_refine_and_reply(
        refine_lazy,
        sub,
        group_id,
        label,
        result,
        allow_old_fallback_on_insufficient=True,
    )


@refine_force.handle()
async def _force(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    """强制炼化：跳过新鲜检查与冷却，每次都重炼。"""
    label = args.extract_plain_text().strip()
    if not label:
        await refine_force.finish("用法：强制炼化 <标签>")
        return

    group_id = str(event.group_id)
    sub = await get_subscription_by_label(group_id, label)
    if sub is None:
        await refine_force.finish(f"未找到标签为「{label}」的订阅")
        return

    try:
        validate_ai_config(config)
    except RefineConfigError as e:
        await refine_force.finish(f"❌ AI 配置缺失：{e}")
        return

    await refine_force.send(f"⏳ 正在为 [{label}] 强制提炼，请稍候...")

    result = await get_result(sub.id)

    await _run_refine_and_reply(
        refine_force,
        sub,
        group_id,
        label,
        result,
        allow_old_fallback_on_insufficient=False,
    )


@refine_help.handle()
async def _help() -> None:
    lines = [
        "🧪 炼化插件帮助",
        "",
        "订阅某个用户或某集合的发言，按需用 AI 生成简要总结；",
        "结果与订阅一一对应，新结果自动覆盖旧结果。",
        "",
        "命令：",
        "  炼化订阅 <标签> user:<qq>",
        "  炼化订阅 <标签> collection:<名>",
        "  炼化订阅 <标签> 集合 <名>",
        "  炼化订阅 <标签> @某人",
        "  炼化订阅列表",
        "  炼化取消订阅 <标签>",
        "  炼化 <标签>          # 缓存新鲜直接返回，过期才重炼",
        "  强制炼化 <标签>      # 跳过新鲜检查与冷却，强制重炼",
    ]
    await refine_help.finish("\n".join(lines))
