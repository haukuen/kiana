"""word_pulse 本地真实 LLM 联调 smoke 脚本。

用法（不接入真 QQ，用 NoneBug mock bot 模式 + 真实 LLM API）：

    export WORD_PULSE_BASE_URL="https://api.exusiai.top/v1"
    export WORD_PULSE_API_KEY="sk-..."
    export WORD_PULSE_MODEL="gpt-4o-mini"   # 或其他 OpenAI 兼容模型
    uv run python scripts/word_pulse_smoke.py

验证内容：
  1. 注册主题 → 真实 LLM 字符集扩展（看字符是否相关、schema strict 是否被接受）
  2. 预筛真实感群消息（看强/弱/灰区分布）
  3. 灰区批量分类 → 真实 LLM 分类（看准确率）
  4. 汇总 → 真实 LLM 总结（看 trend/examples 质量）
  5. 完整渲染输出（人工检视可读性）

所有数据在临时 SQLite 里，跑完即弃，不影响真实 message_archive。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# 把项目根加入 sys.path（让 `from src.plugins...` 能 import）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 必须在 import nonebot 前设置 KIANA_DB_PATH，让 get_db() 指向临时库
_TMP_DB = Path(tempfile.gettempdir()) / "word_pulse_smoke.sqlite3"
os.environ["KIANA_DB_PATH"] = str(_TMP_DB)
for suffix in ("", "-shm", "-wal"):
    Path(f"{_TMP_DB}{suffix}").unlink(missing_ok=True)


def _check_env() -> tuple[str, str, str]:
    base = os.environ.get("WORD_PULSE_BASE_URL", "").rstrip("/")
    key = os.environ.get("WORD_PULSE_API_KEY", "")
    model = os.environ.get("WORD_PULSE_MODEL", "")
    missing = [n for n, v in [("WORD_PULSE_BASE_URL", base), ("WORD_PULSE_API_KEY", key), ("WORD_PULSE_MODEL", model)] if not v]
    if missing:
        print(f"❌ 缺少环境变量: {', '.join(missing)}", file=sys.stderr)
        print("示例:", file=sys.stderr)
        print("  export WORD_PULSE_BASE_URL='https://api.exusiai.top/v1'", file=sys.stderr)
        print("  export WORD_PULSE_API_KEY='sk-...'", file=sys.stderr)
        print("  export WORD_PULSE_MODEL='gpt-4o-mini'", file=sys.stderr)
        sys.exit(1)
    if not base.endswith("/v1"):
        print(f"⚠ base_url 似乎不以 /v1 结尾: {base}（脚本会自动补 /chat/completions）", file=sys.stderr)
    return base, key, model


# ── 真实感测试消息（覆盖三类：精确命中、灰区、无关）──────────────────────────

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _ts(days_ago: int, hour: int, minute: int = 0) -> int:
    """构造相对今天 days_ago 天前 hour:minute 的 epoch 秒（SHANGHAI_TZ）。"""
    now = datetime.now(SHANGHAI_TZ)
    target = now - timedelta(days=days_ago)
    dt = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return int(dt.timestamp())


# 群消息样本：每条 (user_id, sender_name, plain_text, days_ago, hour)
# 故意覆盖炒股主题下的多种表达方式
SAMPLE_MESSAGES: list[tuple[int, str, str, int, int]] = [
    # ── 精确命中"涨停"（弱预筛直接归类）──
    (1001, "股神A", "今天又涨停了哈哈哈", 0, 10),
    (1002, "小韭菜", "涨停涨停涨停！发财了", 0, 14),
    (1003, "老王", "昨天那个涨停板真猛", 1, 9),
    (1004, "路人甲", "这波涨停能持续吗", 1, 20),

    # ── 精确命中"割肉" ──
    (1005, "亏成狗", "今天割肉了，亏死了", 0, 11),
    (1006, "绝望哥", "割肉跑路再见股市", 0, 15),
    (1007, "菜鸡", "昨天没割肉今天更亏", 1, 10),

    # ── 精确命中"抄底" ──
    (1008, "胆大", "抄底抄底！现在就是底", 0, 13),
    (1009, "乐观姐", "我准备抄底了", 1, 14),

    # ── 灰区：含相关字符（亏/跌/涨/板）但无精确种子词 ──
    (1010, "郁闷哥", "今天亏爆了，心态崩了", 0, 14),       # 含"亏"
    (1011, "老张", "跌妈不认，这行情真没法玩", 0, 15),       # 含"跌"
    (1012, "吃瓜", "一字板根本买不到", 1, 10),               # 含"板"
    (1013, "新手", "打板失败被套", 1, 11),                   # 含"板"
    (1014, "佛系", "又亏了3个点，习惯就好", 1, 14),           # 含"亏"
    (1015, "股市老炮", "连板5天太强了", 0, 9),               # 含"板"
    (1016, "贪心", "涨太高了不敢追", 0, 16),                 # 含"涨"
    (1017, "稳健", "今天加仓了点", 1, 13),                   # 含"加"... 可能在"抄底"扩展字符集里

    # ── 无关（强预筛应跳过）──
    (1018, "吃货", "中午吃啥", 0, 12),
    (1019, "上班族", "今天好困啊", 0, 9),
    (1020, "摸鱼王", "下班了下班了", 0, 18),
    (1021, "游戏宅", "晚上开黑吗", 1, 19),
    (1022, "健身狗", "刚跑完5公里", 0, 7),
    (1023, "铲屎官", "我家猫又吐毛球了", 1, 8),
]


async def _init_nonebot(base_url: str, api_key: str, model: str) -> None:
    """初始化 NoneBot 并加载 word_pulse + message_archive 插件。"""
    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter as ONEBOT_V11Adapter

    nonebot.init(
        driver="~fastapi",
        word_pulse_plugin_enabled=True,
        word_pulse_base_url=base_url,
        word_pulse_api_key=api_key,
        word_pulse_model=model,
        word_pulse_group_mode="all",
        superusers=["999"],
    )
    driver = nonebot.get_driver()
    driver.register_adapter(ONEBOT_V11Adapter)
    nonebot.load_plugin("src.plugins.message_archive")
    nonebot.load_plugin("src.plugins.word_pulse")


async def _seed_messages(group_id: int = 654321) -> int:
    """把 SAMPLE_MESSAGES 写入 message_archive 表。"""
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
    from nonebot.adapters.onebot.v11.event import Sender

    from src.plugins.message_archive.db import archive_message_event, ensure_schema

    # driver.on_startup 在脚本里不会自动触发，手动建表
    ensure_schema()

    count = 0
    for i, (uid, name, text, days_ago, hour) in enumerate(SAMPLE_MESSAGES):
        ev = GroupMessageEvent(
            time=_ts(days_ago, hour),
            self_id=987654321,
            post_type="message",
            sub_type="normal",
            user_id=uid,
            message_type="group",
            group_id=group_id,
            message_id=5000 + i,
            message=Message(text),
            original_message=Message(text),
            raw_message=text,
            font=0,
            sender=Sender(user_id=uid, nickname=name, card=name, role="member"),
        )
        await archive_message_event(ev)
        count += 1
    return count


async def _run_pipeline(group_id: int = 654321) -> None:
    """端到端跑：注册主题 → 预筛分析 → 灰区 LLM 分类 → 汇总。"""
    from src.plugins.message_archive.db import fetch_group_messages_by_time_range
    from src.plugins.word_pulse import config
    from src.plugins.word_pulse.ai import classify_batch, expand_charsets, summarize
    from src.plugins.word_pulse.analysis import (
        build_summary_prompt,
        classify_message,
        compute_or_load_buckets,
    )
    from src.plugins.word_pulse.config import (
        CLASSIFY_TEMPERATURE,
        MAX_EXAMPLES_IN_SUMMARY,
        REQUEST_TIMEOUT_SECONDS,
        SUMMARY_TEMPERATURE,
        TODAY_BUCKET_FRESH_SECONDS,
    )
    from src.plugins.word_pulse.db import (
        ensure_schema,
        get_clusters,
        get_expanded_charset,
        replace_clusters,
        save_charsets,
        upsert_theme,
    )

    ensure_schema()

    # ── Step 1: 注册主题 + 真实字符集扩展 ──────────────────────────────
    print("\n" + "=" * 70)
    print("Step 1: 注册主题「炒股」并真实扩展字符集")
    print("=" * 70)
    theme_name = "炒股"
    seeds = ["抄底", "割肉", "涨停"]
    tid = await upsert_theme(str(group_id), theme_name)
    await replace_clusters(tid, seeds)
    print(f"  主题已创建 (theme_id={tid}), seeds={seeds}")
    print(f"  调用 LLM 扩展字符集 (model={config.word_pulse_model})...")

    try:
        charsets = await expand_charsets(
            base_url=config.word_pulse_base_url,
            api_key=config.word_pulse_api_key,
            model=config.word_pulse_model,
            seeds=seeds,
            theme=theme_name,
            temperature=CLASSIFY_TEMPERATURE,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except Exception as e:
        print(f"  ❌ 字符集扩展失败: {type(e).__name__}: {e}")
        print("  后续步骤会用空字符集降级（只靠种子词字符预筛）")
        charsets = {}
    else:
        await save_charsets(tid, charsets)
        print("  ✓ 字符集扩展成功:")
        for cl, chars in charsets.items():
            print(f"    {cl}: {chars}")

    # ── Step 2: 预筛分析（不调 LLM）──────────────────────────────────
    print("\n" + "=" * 70)
    print("Step 2: 三级预筛分析（0 LLM 调用）")
    print("=" * 70)

    # 取今天 + 昨天 的消息
    now_dt = datetime.now(SHANGHAI_TZ)
    today_start = int(datetime.combine(now_dt.date(), __import__("datetime").time.min, SHANGHAI_TZ).timestamp())
    yesterday_start = today_start - 86400
    today_end = today_start + 86400

    msgs_today = await fetch_group_messages_by_time_range(str(group_id), today_start, today_end)
    msgs_yesterday = await fetch_group_messages_by_time_range(str(group_id), yesterday_start, today_start)
    all_msgs = msgs_yesterday + msgs_today
    print(f"  取到 {len(msgs_yesterday)} 条昨日 + {len(msgs_today)} 条今日 = {len(all_msgs)} 条消息")

    # 构建 char_pool
    clusters = await get_clusters(tid)
    cluster_names = [c["name"] for c in clusters]
    seed_chars = set("".join(cluster_names))
    expanded = await get_expanded_charset(tid)
    char_pool = seed_chars | expanded
    cluster_terms = {n: {n} for n in cluster_names}
    print(f"  char_pool ({len(char_pool)} 字): {''.join(sorted(char_pool))}")

    direct: list[tuple[int, list[str]]] = []
    grey: list[tuple[int, str]] = []
    skipped: list[tuple[int, str]] = []
    for m in all_msgs:
        r = classify_message(m.plain_text, char_pool, cluster_terms)
        if r is None:
            skipped.append((m.id, m.plain_text))
        elif r == "GREY":
            grey.append((m.id, m.plain_text))
        else:
            direct.append((m.id, r))

    print("\n  📊 预筛分布:")
    print(f"    强预筛跳过（无关）: {len(skipped)} 条")
    for mid, txt in skipped[:5]:
        print(f"      [{mid}] {txt}")
    if len(skipped) > 5:
        print(f"      ... (共 {len(skipped)} 条)")

    print(f"\n    弱预筛直接归类（精确命中）: {len(direct)} 条")
    for mid, cls in direct:
        txt = next((m.plain_text for m in all_msgs if m.id == mid), "?")
        print(f"      [{mid}] {txt}  →  {cls}")

    print(f"\n    灰区（送 LLM 分类）: {len(grey)} 条")
    for mid, txt in grey:
        print(f"      [{mid}] {txt}")

    # ── Step 3: 灰区真实 LLM 分类 ────────────────────────────────────
    if grey:
        print("\n" + "=" * 70)
        print(f"Step 3: 灰区 {len(grey)} 条 → 真实 LLM 批量分类")
        print("=" * 70)
        try:
            grey_results = await classify_batch(
                base_url=config.word_pulse_base_url,
                api_key=config.word_pulse_api_key,
                model=config.word_pulse_model,
                messages=grey,
                clusters=[{"id": i, "name": n} for i, n in enumerate(cluster_names)],
                theme_name=theme_name,
                temperature=CLASSIFY_TEMPERATURE,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as e:
            print(f"  ❌ 灰区分类失败: {type(e).__name__}: {e}")
            grey_results = [(mid, None) for mid, _ in grey]
        else:
            print("  ✓ LLM 分类结果:")
            for mid, cl in grey_results:
                txt = next((m.plain_text for m in all_msgs if m.id == mid), "?")
                marker = "✓" if cl else "✗"
                print(f"    {marker} [{mid}] {txt}  →  {cl or '(null 不归类)'}")
    else:
        grey_results = []
        print("\n  ℹ 无灰区消息需要 LLM 分类")

    # ── Step 4: 通过 compute_or_load_buckets 跑完整日桶计算 ──────────
    print("\n" + "=" * 70)
    print("Step 4: compute_or_load_buckets 完整日桶计算（2 天）")
    print("=" * 70)
    buckets = await compute_or_load_buckets(
        group_id=str(group_id), theme_id=tid, theme_name=theme_name,
        clusters=[{"id": i, "name": n} for i, n in enumerate(cluster_names)],
        char_pool=char_pool, cluster_terms=cluster_terms,
        day_range=2,
        base_url=config.word_pulse_base_url, api_key=config.word_pulse_api_key,
        model=config.word_pulse_model,
        max_messages_per_bucket=config.word_pulse_max_messages_per_bucket,
        max_sample_per_cluster=config.word_pulse_max_sample_per_cluster,
        temperature=CLASSIFY_TEMPERATURE,
        timeout=REQUEST_TIMEOUT_SECONDS,
        today_bucket_fresh_seconds=TODAY_BUCKET_FRESH_SECONDS,
    )
    for b in buckets:
        sampled_tag = " (sampled)" if b.get("sampled") else ""
        print(f"\n  [{b['day']}] 总 {b['total_messages']} 条{sampled_tag}")
        for k, v in sorted(b["counts"].items()):
            print(f"    {k}: {v}")
        if b["samples"]:
            print("    抽样原文:")
            for s in b["samples"][:3]:
                print(f"      · {s['author']}: {s['text']} (→ {s['cluster']})")

    # ── Step 5: 真实 LLM 汇总 + 渲染 ─────────────────────────────────
    print("\n" + "=" * 70)
    print("Step 5: 真实 LLM 汇总 + 完整渲染")
    print("=" * 70)

    start = (now_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    end = now_dt.strftime("%Y-%m-%d")
    window_desc = f"2天 ({start} ~ {end})"
    prompt = build_summary_prompt(
        theme_name=theme_name,
        clusters=[{"name": n} for n in cluster_names],
        buckets=buckets,
        window_meta=window_desc,
        max_examples=MAX_EXAMPLES_IN_SUMMARY,
    )
    print("\n  ───── 送给 LLM 的 prompt ─────")
    print(prompt)
    print("  ────────────────────────────────")

    try:
        summary = await summarize(
            base_url=config.word_pulse_base_url,
            api_key=config.word_pulse_api_key,
            model=config.word_pulse_model,
            prompt=prompt,
            temperature=SUMMARY_TEMPERATURE,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except Exception as e:
        print(f"\n  ❌ 汇总失败: {type(e).__name__}: {e}")
        return

    print("\n  ✓ LLM 返回:")
    print(f"    trend: {summary.trend}")
    print("    ranking:")
    for r in summary.ranking:
        print(f"      {r.cluster}: {r.count} ({r.percent}%)")
    print("    examples:")
    for ex in summary.examples:
        print(f"      [{ex.day}] {ex.author}: {ex.text} (→ {ex.cluster})")
    if summary.unclassified_high_freq:
        print("    unclassified_high_freq:")
        for t in summary.unclassified_high_freq:
            print(f"      {t.term}: {t.count}")

    # ── Step 6: 完整渲染输出（模拟用户看到的）────────────────────────
    print("\n" + "=" * 70)
    print("Step 6: 用户最终看到的渲染输出")
    print("=" * 70)
    from src.plugins.word_pulse import _render_summary
    rendered = _render_summary(theme_name, window_desc, buckets, summary)
    print()
    print(rendered)
    print()


async def main() -> None:
    base, key, model = _check_env()
    print("🚀 word_pulse smoke 联调")
    print(f"   base_url: {base}")
    print(f"   model:    {model}")
    print(f"   db:       {_TMP_DB}")
    print(f"   样本消息: {len(SAMPLE_MESSAGES)} 条（今日 + 昨日）")

    await _init_nonebot(base, key, model)
    seeded = await _seed_messages()
    print(f"   已写入 message_archive: {seeded} 条")

    await _run_pipeline()

    # 清理临时 db
    try:
        from src.storage import get_db
        await get_db().close()
    except Exception:
        pass
    print("\n✅ smoke 完成。临时 db 在", _TMP_DB)


if __name__ == "__main__":
    asyncio.run(main())
