from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from nonebot import get_driver, get_plugin_config, logger, on_regex, require
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.exception import MatcherException
from nonebot.plugin import PluginMetadata

from src.plugins.group_permission import create_group_rule

from .ai import (
    WordPulseAIAuthError,
    WordPulseAIError,
    WordPulseAIResponseError,
    WordPulseAIServiceError,
    WordPulseAITimeoutError,
    expand_charsets,
    summarize,
)
from .analysis import build_summary_prompt, compute_or_load_buckets
from .commands import parse_command, parse_query, resolve_window_days
from .config import (
    CLASSIFY_TEMPERATURE,
    MAX_EXAMPLES_IN_SUMMARY,
    QUERY_COOLDOWN_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    RESULT_CACHE_TTL_SECONDS,
    SUMMARY_TEMPERATURE,
    TODAY_BUCKET_FRESH_SECONDS,
    Config,
)
from .db import (
    add_cluster_aliases,
    add_clusters,
    delete_buckets_by_theme,
    delete_cluster,
    delete_theme,
    ensure_schema,
    get_bucket_days,
    get_clusters,
    get_expanded_charset,
    get_theme,
    list_themes,
    remove_cluster_aliases,
    replace_clusters,
    save_charsets,
    upsert_theme,
)

scheduler = require("nonebot_plugin_apscheduler").scheduler

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

__plugin_meta__ = PluginMetadata(
    name="word_pulse",
    description="群聊主题词频统计 — 注册主题 → 日桶 AI 分类 → 热度总结",
    usage=(
        "词频 add 主题 种子词... —— 创建主题\n"
        "词频 append 主题 种子词... —— 追加子类\n"
        "词频 list —— 列出本群主题\n"
        "词频 del 主题 —— 删除主题\n"
        "词频 del 主题 子类 —— 删除子类\n"
        "词频 refresh 主题 —— 刷新字符集\n"
        "词频 alias 主题 主名词 别名1 别名2... —— 给子类添加别名\n"
        "词频 unalias 主题 主名词 别名1... —— 删除子类别名\n"
        "总结 N天/周/月 主题 —— 查询热度统计"
    ),
    config=Config,
)

config: Config = get_plugin_config(Config)
driver = get_driver()
SUPERUSERS: set[str] = set(driver.config.superusers)


@driver.on_startup
async def _init_word_pulse_schema() -> None:
    """启动时初始化表结构。"""
    ensure_schema()


# ── Permission rule ──

word_pulse_rule = create_group_rule(lambda: config, "word_pulse_plugin_enabled", "word_pulse_")

# ── Matchers ──

admin_matcher = on_regex(
    r"^词频\s+(add|append|list|del|refresh|alias|unalias)\b.*",
    rule=word_pulse_rule, priority=5, block=True,
)
query_matcher = on_regex(r"^总结\s+\d+\s*(天|d|周|w|月|m)\s+\S+", rule=word_pulse_rule, priority=5, block=True)
# 帮助命令所有人可用，独立 matcher，不走 handle_admin 的管理员校验。
# 正则锚定整条消息，避免「词频帮助我」之类的粘连文本误触发。
help_matcher = on_regex(r"^词频\s+(help|帮助)\s*$", rule=word_pulse_rule, priority=5, block=True)

# ── Cooldown & Cache ──

cooldown_dict: dict[str, float] = {}


@dataclass(slots=True)
class CachedResult:
    created_at: float
    response_text: str


result_cache: dict[str, CachedResult] = {}
_cache_ttl = RESULT_CACHE_TTL_SECONDS


def _cache_key(gid: int, theme: str, sig: str) -> str:
    return hashlib.md5(f"{gid}:{theme}:{sig}".encode(), usedforsecurity=False).hexdigest()


def _get_cached(key: str) -> str | None:
    now = time.time()
    for k in list(result_cache.keys()):
        if now - result_cache[k].created_at >= _cache_ttl:
            del result_cache[k]
    cr = result_cache.get(key)
    return cr.response_text if cr else None


def _set_cached(key: str, text: str) -> None:
    result_cache[key] = CachedResult(created_at=time.time(), response_text=text)


def _cooldown_remaining(gid: int) -> int:
    last = cooldown_dict.get(str(gid))
    if last is None:
        return 0
    return max(0, int(last + QUERY_COOLDOWN_SECONDS - time.time()))


def _mark_cooldown(gid: int) -> None:
    cooldown_dict[str(gid)] = time.time()


def _validate_config() -> str | None:
    if not config.word_pulse_base_url.strip():
        return "词频插件未配置 base_url"
    if not config.word_pulse_api_key.strip():
        return "词频插件未配置 api_key"
    if not config.word_pulse_model.strip():
        return "词频插件未配置 model"
    return None


def _is_admin(event: GroupMessageEvent) -> bool:
    if str(event.user_id) in SUPERUSERS:
        return True
    return (event.sender.role if event.sender else "member") in ("owner", "admin")


async def _ensure_group(event: MessageEvent) -> GroupMessageEvent:
    if not isinstance(event, GroupMessageEvent):
        await admin_matcher.finish("仅支持群聊使用")
    return event


# ── Admin handlers ──


async def _run_expand_or_degrade(*, theme_id: int, seeds: list[str], theme_name: str, gid: str) -> str | None:
    """调用 expand_charsets 并保存；失败时返回降级提示文案，成功返回 None。"""
    try:
        charsets = await expand_charsets(
            base_url=config.word_pulse_base_url, api_key=config.word_pulse_api_key,
            model=config.word_pulse_model, seeds=seeds, theme=theme_name,
            temperature=CLASSIFY_TEMPERATURE, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        await save_charsets(theme_id, charsets)
        await delete_buckets_by_theme(gid, theme_id)
        return None
    except WordPulseAIError as e:
        logger.warning(f"[词频] 字符集扩展失败（已降级）: {e}")
        return (
            f"✓ 主题「{theme_name}」已创建（字符集扩展失败，已降级为纯精确匹配，"
            f"可稍后重试「词频 refresh {theme_name}」）"
        )


@admin_matcher.handle()
async def handle_admin(event: MessageEvent) -> None:
    ge = await _ensure_group(event)
    cmd = parse_command(event.raw_message.strip())
    if cmd is None:
        await admin_matcher.finish("命令格式错误")
        return
    if cmd.action != "list" and not _is_admin(ge):
        await admin_matcher.finish("仅群管理或 Bot 管理员可使用此命令")
        return
    handlers = {
        "add": _handle_add, "append": _handle_append, "list": _handle_list,
        "del": _handle_del, "refresh": _handle_refresh,
        "alias": _handle_alias, "unalias": _handle_unalias,
    }
    try:
        await handlers[cmd.action](ge, cmd)
    except MatcherException:
        # NoneBot 控制流异常（如 FinishedException）必须向上抛出，不能吞掉
        raise
    except Exception as e:
        logger.error(f"[词频] {cmd.action} 失败: {e}", exc_info=True)
        await admin_matcher.finish(f"操作失败：{e}")


# ── Help handler ──


@help_matcher.handle()
async def handle_help() -> None:
    lines = [
        "📊 词频插件帮助",
        "",
        "群聊主题词频统计 — 创建主题 → AI 日桶分类 → 热度总结。",
        "",
        "管理命令(需群管理员/Bot 管理员):",
        "  词频 add <主题> <种子词1> <种子词2> ...   创建主题",
        "  词频 append <主题> <种子词...>            追加子类",
        "  词频 list                                 列出本群主题",
        "  词频 del <主题>                           删除主题",
        "  词频 del <主题> <子类>                    删除子类",
        "  词频 refresh <主题>                       刷新字符集(AI 扩展)",
        "  词频 alias <主题> <主名词> <别名...>      给子类加别名",
        "  词频 unalias <主题> <主名词> <别名...>    删除子类别名",
        "",
        "查询命令(所有人可用):",
        "  总结 <N>天/周/月 <主题>                  查询热度统计(例: 总结 7天 炒股)",
        "",
        "时间单位: 天/d, 周/w, 月/m",
    ]
    await help_matcher.finish("\n".join(lines))


async def _handle_add(event: GroupMessageEvent, cmd) -> None:
    gid = str(event.group_id)
    tid = await upsert_theme(gid, cmd.theme)
    await replace_clusters(tid, cmd.seeds)
    degraded = await _run_expand_or_degrade(theme_id=tid, seeds=cmd.seeds, theme_name=cmd.theme, gid=gid)
    if degraded is not None:
        await admin_matcher.finish(degraded)
        return
    await admin_matcher.finish(f"✓ 主题「{cmd.theme}」已创建（覆盖式）\n  子类：{'、'.join(cmd.seeds)}")


async def _handle_append(event: GroupMessageEvent, cmd) -> None:
    gid = str(event.group_id)
    theme = await get_theme(gid, cmd.theme)
    if theme is None:
        await admin_matcher.finish(f"本群尚未创建主题「{cmd.theme}」")
        return
    new_ids = await add_clusters(theme["id"], cmd.seeds)
    if not new_ids:
        await admin_matcher.finish(f"子类「{'、'.join(cmd.seeds)}」已存在")
        return
    new_seeds = [cmd.seeds[i] for i in range(len(cmd.seeds)) if i < len(new_ids)]
    try:
        charsets = await expand_charsets(
            base_url=config.word_pulse_base_url, api_key=config.word_pulse_api_key,
            model=config.word_pulse_model, seeds=new_seeds, theme=cmd.theme,
            temperature=CLASSIFY_TEMPERATURE, timeout=REQUEST_TIMEOUT_SECONDS,
        )
        await save_charsets(theme["id"], charsets)
    except WordPulseAIError as e:
        logger.warning(f"[词频] append 字符集扩展失败: {e}")
    await delete_buckets_by_theme(gid, theme["id"])
    await admin_matcher.finish(f"✓ 已追加子类到「{cmd.theme}」：{'、'.join(cmd.seeds)}")


async def _handle_list(event: GroupMessageEvent, _cmd=None) -> None:
    themes = await list_themes(str(event.group_id))
    if not themes:
        await admin_matcher.finish("本群暂无主题")
        return
    lines = ["📊 本群主题列表："]
    for t in themes:
        cls = await get_clusters(t["id"])
        if not cls:
            lines.append(f"  · {t['name']} — （无子类）")
            continue
        for c in cls:
            aliases = c.get("aliases") or []
            if aliases:
                lines.append(f"  · {t['name']} / {c['name']}（别名: {'、'.join(aliases)}）")
            else:
                lines.append(f"  · {t['name']} / {c['name']}")
    await admin_matcher.finish("\n".join(lines))


async def _handle_del(event: GroupMessageEvent, cmd) -> None:
    theme = await get_theme(str(event.group_id), cmd.theme)
    if theme is None:
        await admin_matcher.finish(f"本群尚未创建主题「{cmd.theme}」")
        return
    if cmd.seeds:
        cls = await get_clusters(theme["id"])
        target = [c for c in cls if c["name"] == cmd.seeds[0]]
        if not target:
            await admin_matcher.finish(f"主题「{cmd.theme}」中没有子类「{cmd.seeds[0]}」")
            return
        await delete_cluster(target[0]["id"])
        await delete_buckets_by_theme(str(event.group_id), theme["id"])
        await admin_matcher.finish(f"✓ 已从「{cmd.theme}」删除子类「{cmd.seeds[0]}」")
        return
    await delete_theme(theme["id"])
    await admin_matcher.finish(f"✓ 已删除主题「{cmd.theme}」")


async def _handle_refresh(event: GroupMessageEvent, cmd) -> None:
    theme = await get_theme(str(event.group_id), cmd.theme)
    if theme is None:
        await admin_matcher.finish(f"本群尚未创建主题「{cmd.theme}」")
        return
    cls = await get_clusters(theme["id"])
    if not cls:
        await admin_matcher.finish(f"主题「{cmd.theme}」没有任何子类")
        return
    seeds = [c["name"] for c in cls]
    degraded = await _run_expand_or_degrade(theme_id=theme["id"], seeds=seeds, theme_name=cmd.theme, gid=str(event.group_id))
    if degraded is not None:
        await admin_matcher.finish("字符集刷新失败，请稍后重试或检查 AI 配置")
        return
    await admin_matcher.finish(f"✓ 主题「{cmd.theme}」字符集已刷新（{len(seeds)} 个子类）\n  子类：{'、'.join(seeds)}")


async def _handle_alias(event: GroupMessageEvent, cmd) -> None:
    """词频 alias 主题 主名词 别名... —— 给子类批量添加别名。

    cmd.seeds[0] = 主名词，cmd.seeds[1:] = 别名列表。
    """
    theme = await get_theme(str(event.group_id), cmd.theme)
    if theme is None:
        await admin_matcher.finish(f"本群尚未创建主题「{cmd.theme}」")
        return
    main_name, aliases = cmd.seeds[0], cmd.seeds[1:]
    cls = await get_clusters(theme["id"])
    target = [c for c in cls if c["name"] == main_name]
    if not target:
        await admin_matcher.finish(f"主题「{cmd.theme}」中没有子类「{main_name}」")
        return
    added = await add_cluster_aliases(target[0]["id"], aliases, theme_id=theme["id"])
    # alias 变了，旧的日桶归类已过期，清掉让其重算
    await delete_buckets_by_theme(str(event.group_id), theme["id"])
    if not added:
        await admin_matcher.finish(
            f"未新增别名（全部为重复或与现有子类名冲突）：{'、'.join(aliases)}"
        )
        return
    await admin_matcher.finish(
        f"✓ 已为「{main_name}」添加别名：{'、'.join(added)}"
    )


async def _handle_unalias(event: GroupMessageEvent, cmd) -> None:
    """词频 unalias 主题 主名词 别名... —— 删除子类别名。"""
    theme = await get_theme(str(event.group_id), cmd.theme)
    if theme is None:
        await admin_matcher.finish(f"本群尚未创建主题「{cmd.theme}」")
        return
    main_name, aliases = cmd.seeds[0], cmd.seeds[1:]
    cls = await get_clusters(theme["id"])
    target = [c for c in cls if c["name"] == main_name]
    if not target:
        await admin_matcher.finish(f"主题「{cmd.theme}」中没有子类「{main_name}」")
        return
    removed = await remove_cluster_aliases(target[0]["id"], aliases)
    await delete_buckets_by_theme(str(event.group_id), theme["id"])
    if not removed:
        await admin_matcher.finish(f"未删除任何别名（不存在的别名被忽略）：{'、'.join(aliases)}")
        return
    await admin_matcher.finish(f"✓ 已从「{main_name}」删除别名：{'、'.join(removed)}")


# ── Query handler ──


async def _resolve_query_target(ge: GroupMessageEvent, query) -> tuple[dict, list[dict], str] | str:
    """解析主题/子类/字符池，返回 (theme, clusters, cache_key) 或错误文案。"""
    gid = str(ge.group_id)
    theme = await get_theme(gid, query.theme)
    if theme is None:
        return f"本群尚未创建主题「{query.theme}」"
    clusters = await get_clusters(theme["id"])
    if not clusters:
        return f"主题「{query.theme}」没有任何子类"
    bucket_days = await get_bucket_days(gid, theme["id"])
    sig = hashlib.md5("".join(sorted(bucket_days)).encode(), usedforsecurity=False).hexdigest()[:8]
    ck = _cache_key(ge.group_id, query.theme, f"{query.window_value}{query.window_unit}:{sig}")
    return theme, clusters, ck


async def _run_summary(*, query, theme: dict, clusters: list[dict], buckets: list[dict], window_desc: str) -> str:
    prompt = build_summary_prompt(
        theme_name=query.theme, clusters=clusters, buckets=buckets,
        window_meta=window_desc, max_examples=MAX_EXAMPLES_IN_SUMMARY,
    )
    try:
        summary = await summarize(
            base_url=config.word_pulse_base_url, api_key=config.word_pulse_api_key,
            model=config.word_pulse_model, prompt=prompt,
            temperature=SUMMARY_TEMPERATURE, timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except WordPulseAITimeoutError:
        return "AI 总结超时，请稍后重试"
    except WordPulseAIAuthError:
        return "AI 鉴权失败，请检查 API Key"
    except WordPulseAIResponseError:
        return "AI 返回格式异常"
    except WordPulseAIServiceError as e:
        logger.error(f"[词频] AI 服务异常: {e}", exc_info=True)
        return "AI 服务暂时不可用"
    except Exception as e:
        logger.error(f"[词频] 总结失败: {e}", exc_info=True)
        return "AI 总结失败"
    return _render_summary(query.theme, window_desc, buckets, summary)


@query_matcher.handle()
async def handle_query(event: MessageEvent) -> None:
    # bug#4 诊断:确认 handler 被触发 + raw_message 实际内容（含不可见字符）
    logger.info(f"[词频] query 进入 handler: raw_message={event.raw_message!r}")
    ge = await _ensure_group(event)
    query = parse_query(event.raw_message.strip())
    if query is None:
        # parse 失败但 matcher 触发了（regex 与 parse_query 末尾 $ 锚点差异），
        # 这种"半静默"路径用户最困惑，单独打 warning
        logger.warning(f"[词频] query 解析失败: raw={event.raw_message!r}")
        await query_matcher.finish("查询格式错误")
        return
    cfg_err = _validate_config()
    if cfg_err:
        await query_matcher.finish(cfg_err)
        return
    total_days = resolve_window_days(query.window_value, query.window_unit, config.word_pulse_max_window_days)
    if total_days is None:
        await query_matcher.finish(f"时间范围超过上限 {config.word_pulse_max_window_days} 天")
        return
    resolved = await _resolve_query_target(ge, query)
    if isinstance(resolved, str):
        await query_matcher.finish(resolved)
        return
    theme, clusters, ck = resolved
    cached = _get_cached(ck)
    if cached:
        await query_matcher.finish(cached)
        return
    remaining = _cooldown_remaining(ge.group_id)
    if remaining > 0:
        await query_matcher.finish(f"冷却中，请等待 {remaining} 秒")
        return
    _mark_cooldown(ge.group_id)
    buckets = await _compute_buckets(ge, query, theme, clusters, total_days)
    window_desc = _build_window_desc(query, total_days)
    resp = await _run_summary(query=query, theme=theme, clusters=clusters, buckets=buckets, window_desc=window_desc)
    _set_cached(ck, resp)
    await query_matcher.finish(resp)


async def _compute_buckets(ge: GroupMessageEvent, query, theme: dict, clusters: list[dict], total_days: int) -> list[dict]:
    seed_chars = set("".join(c["name"] for c in clusters))
    # 主名词 + 别名 都参与粗筛字符池（别名里的字也算相关字）
    alias_chars = set("".join("".join(c.get("aliases") or []) for c in clusters))
    expanded = await get_expanded_charset(theme["id"])
    char_pool = seed_chars | alias_chars | expanded
    # 关键：cluster_terms 把别名并入主名词，使别名消息在弱预筛阶段就被正确归类
    cluster_terms = {
        c["name"]: {c["name"], *(c.get("aliases") or [])} for c in clusters
    }
    # 给 GREY 阶段 classify_batch 用的 cluster 结构必须含 aliases
    clusters_for_classify = [
        {"name": c["name"], "aliases": c.get("aliases") or []} for c in clusters
    ]
    return await compute_or_load_buckets(
        group_id=str(ge.group_id), theme_id=theme["id"], theme_name=query.theme,
        clusters=clusters_for_classify, char_pool=char_pool, cluster_terms=cluster_terms,
        day_range=total_days,
        base_url=config.word_pulse_base_url, api_key=config.word_pulse_api_key, model=config.word_pulse_model,
        max_messages_per_bucket=config.word_pulse_max_messages_per_bucket,
        max_sample_per_cluster=config.word_pulse_max_sample_per_cluster,
        temperature=CLASSIFY_TEMPERATURE, timeout=REQUEST_TIMEOUT_SECONDS,
        today_bucket_fresh_seconds=TODAY_BUCKET_FRESH_SECONDS,
    )


def _build_window_desc(query, total_days: int) -> str:
    now_dt = datetime.now(SHANGHAI_TZ)
    start = (now_dt - timedelta(days=total_days - 1)).strftime("%Y-%m-%d")
    end = now_dt.strftime("%Y-%m-%d")
    unit_disp = {"d": "天", "w": "周", "m": "月"}.get(query.window_unit, query.window_unit)
    return f"{query.window_value}{unit_disp} ({start} ~ {end})"


def _render_summary(theme: str, window: str, buckets: list[dict], summary) -> str:
    total: dict[str, int] = {}
    for b in buckets:
        for k, v in b.get("counts", {}).items():
            total[k] = total.get(k, 0) + v
    grand = sum(total.values())
    lines = [f"📊 {theme}主题 · {window}", "━" * 30, "排名  子类     计数   占比"]
    for idx, item in enumerate(summary.ranking, 1):
        lines.append(f"{idx}.   {item.cluster:<6} {item.count:<6} {item.percent}%")
    if "_other" in total:
        pct = "0" if grand == 0 else f"{round(total['_other'] / grand * 100)}%"
        lines.append(f"-     其他      {total['_other']:<6} {pct}")
    lines.append(f"-     跳过      {total.get('_skipped', 0)}")
    lines.append("━" * 30)
    lines.append(f"📈 趋势：{summary.trend}")
    if summary.examples:
        lines.append("💬 典型原文：")
        for ex in summary.examples[:5]:
            lines.append(f"  · \"{ex.text}\" — {ex.author} {ex.day}")
    if summary.unclassified_high_freq:
        terms = "· ".join(f"{t.term}（{t.count}）" for t in summary.unclassified_high_freq[:8])
        lines.append(f"🔍 新发现的可能相关词（未归类）：\n  · {terms}")
    return "\n".join(lines)


# ── APScheduler cleanup ──


@scheduler.scheduled_job("cron", hour=3, minute=0, id="word_pulse_daily_cleanup", misfire_grace_time=300)
async def daily_cleanup() -> None:
    from .db import delete_buckets_older_than  # noqa: PLC0415
    cutoff = (datetime.now(SHANGHAI_TZ) - timedelta(days=config.word_pulse_bucket_retention_days)).strftime("%Y-%m-%d")
    deleted = await delete_buckets_older_than(cutoff)
    if deleted:
        logger.info(f"[词频] 清理了 {deleted} 条过期桶（< {cutoff}）")
    now = time.time()
    expired = [k for k in list(result_cache.keys()) if now - result_cache[k].created_at >= _cache_ttl]
    for k in expired:
        del result_cache[k]
    if expired:
        logger.info(f"[词频] 清理了 {len(expired)} 条缓存")
