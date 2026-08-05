"""word_pulse.analysis.py 的补充单元测试。

与 ``test_word_pulse_analysis.py`` 的差异:
- 该文件原只覆盖 classify_message + classify_batch prompt 白盒验证。
- 本文件补齐:
  * ``classify_message`` 空字符串边界
  * ``uniform_sample`` 各边界(<=0 / 小于 max / 等于 max / 大于 max / max=1)
  * ``_pick_evenly`` 边界
  * ``merge_results`` 直接归类 + 灰区归类 + skipped + _other + max_sample 抽样
  * ``build_summary_prompt`` 有 samples / 无 samples 两套
  * ``_compute_day_bucket`` 有消息 / 无消息 / 消息数 > max(走 sampled_flag)
  * ``compute_or_load_buckets`` 历史日缓存命中 + 今日 freshness 内命中 + 全新计算

只 mock 外部依赖(``_request_llm`` / ``classify_batch``),不 mock 业务逻辑。
不修改任何源代码。
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from nonebug import App

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


# ═══════════════════════════════════════════════════════════════
# classify_message 边界
# ═══════════════════════════════════════════════════════════════


def test_classify_message_empty_returns_none(app: App) -> None:
    """空字符串消息直接返回 None(不进入字符池判断)。"""
    from src.plugins.word_pulse.analysis import classify_message
    assert classify_message("", {"茅"}, {"茅台": {"茅台"}}) is None


def test_classify_message_no_overlap_returns_none(app: App) -> None:
    """消息字符集与 char_pool 无交集返回 None。"""
    from src.plugins.word_pulse.analysis import classify_message
    assert classify_message("中午吃啥", {"茅"}, {"茅台": {"茅台"}}) is None


# ═══════════════════════════════════════════════════════════════
# uniform_sample 边界
# ═══════════════════════════════════════════════════════════════


def _mock_msg(mid: int, et: int = 0):
    """构造一个轻量的 ArchivedMessage-like 对象,只带 uniform_sample 用到的字段。"""
    from src.plugins.message_archive.db import ArchivedMessage
    return ArchivedMessage(
        id=mid, session_type="group", session_id="g1", message_id=mid,
        event_time=et, self_id="1", user_id="u", group_id="g1",
        sender_name="s", message_cq="", plain_text=f"msg{mid}",
    )


def test_uniform_sample_max_le_zero_returns_empty(app: App) -> None:
    """max_count <= 0 时返回空列表。"""
    from src.plugins.word_pulse.analysis import uniform_sample
    msgs = [_mock_msg(1), _mock_msg(2)]
    assert uniform_sample(msgs, 0) == []
    assert uniform_sample(msgs, -1) == []


def test_uniform_sample_fewer_than_max_returns_all(app: App) -> None:
    """消息数小于 max_count 时原样返回。"""
    from src.plugins.word_pulse.analysis import uniform_sample
    msgs = [_mock_msg(1, et=10), _mock_msg(2, et=20)]
    result = uniform_sample(msgs, 5)
    assert [m.id for m in result] == [1, 2]


def test_uniform_sample_equal_to_max_returns_all(app: App) -> None:
    """消息数等于 max_count 时原样返回(已按时间排序)。"""
    from src.plugins.word_pulse.analysis import uniform_sample
    msgs = [_mock_msg(i, et=i * 10) for i in range(1, 4)]
    result = uniform_sample(msgs, 3)
    assert [m.id for m in result] == [1, 2, 3]


def test_uniform_sample_more_than_max_returns_evenly(app: App) -> None:
    """消息数大于 max_count 时返回 max_count 条,均匀分布且去重。"""
    from src.plugins.word_pulse.analysis import uniform_sample
    msgs = [_mock_msg(i, et=i) for i in range(1, 11)]  # 10 条
    result = uniform_sample(msgs, 3)
    assert len(result) == 3
    # 三条 id 各不相同
    ids = [m.id for m in result]
    assert len(set(ids)) == 3
    # 应包含首尾
    assert 1 in ids
    assert 10 in ids


def test_uniform_sample_max_one_returns_middle(app: App) -> None:
    """max_count=1 时取中间那条(避免 ZeroDivisionError)。"""
    from src.plugins.word_pulse.analysis import uniform_sample
    msgs = [_mock_msg(i, et=i) for i in range(1, 6)]  # 5 条
    result = uniform_sample(msgs, 1)
    assert len(result) == 1
    # 中间 index = len//2 = 5//2 = 2,对应 msgs[2].id = 3
    assert result[0].id == 3


# ═══════════════════════════════════════════════════════════════
# _pick_evenly 边界
# ═══════════════════════════════════════════════════════════════


def test_pick_evenly_count_le_zero(app: App) -> None:
    """count <= 0 返回空。"""
    from src.plugins.word_pulse.analysis import _pick_evenly
    assert _pick_evenly([1, 2, 3], 0) == []
    assert _pick_evenly([1, 2, 3], -1) == []


def test_pick_evenly_empty_items(app: App) -> None:
    """items 为空时返回空。"""
    from src.plugins.word_pulse.analysis import _pick_evenly
    assert _pick_evenly([], 3) == []


def test_pick_evenly_count_ge_len_returns_all(app: App) -> None:
    """count >= len(items) 时返回所有元素。"""
    from src.plugins.word_pulse.analysis import _pick_evenly
    assert _pick_evenly([1, 2, 3], 5) == [1, 2, 3]
    assert _pick_evenly([1, 2, 3], 3) == [1, 2, 3]


def test_pick_evenly_picks_endpoints(app: App) -> None:
    """count < len 时,均匀采样含首尾。"""
    from src.plugins.word_pulse.analysis import _pick_evenly
    result = _pick_evenly(list(range(10)), 3)
    assert result[0] == 0  # 首
    assert result[-1] == 9  # 尾
    assert len(result) == 3


# ═══════════════════════════════════════════════════════════════
# merge_results
# ═══════════════════════════════════════════════════════════════


def test_merge_results_direct_only(app: App) -> None:
    """direct 命中的消息计入对应 cluster,samples 含原文。"""
    from src.plugins.word_pulse.analysis import merge_results
    # 构造两条消息
    m1 = _mock_msg(1, et=100)
    m2 = _mock_msg(2, et=200)
    all_msgs = {1: m1, 2: m2}

    direct = [(1, ["茅台"]), (2, ["五粮液"])]
    grey_classified: list[tuple[int, str | None]] = []
    counts, samples = merge_results(direct, grey_classified, all_msgs, "2026-01-01", 0)

    assert counts["茅台"] == 1
    assert counts["五粮液"] == 1
    assert counts["_other"] == 0
    assert counts["_skipped"] == 0
    # 每条 direct 都被收集为 sample
    assert len(samples) == 2
    clusters_in_samples = {s["cluster"] for s in samples}
    assert clusters_in_samples == {"茅台", "五粮液"}
    # samples 字段格式
    s0 = samples[0]
    assert {"cluster", "text", "author", "day", "event_time"} == set(s0.keys())
    assert s0["day"] == "2026-01-01"


def test_merge_results_grey_classified_and_other(app: App) -> None:
    """灰区 LLM 归类为具体 cluster 计入,归类 None 计入 _other。"""
    from src.plugins.word_pulse.analysis import merge_results
    m1 = _mock_msg(1, et=100)
    m2 = _mock_msg(2, et=200)
    all_msgs = {1: m1, 2: m2}

    direct: list[tuple[int, list[str]]] = []
    grey_classified = [(1, "茅台"), (2, None)]
    counts, _samples = merge_results(direct, grey_classified, all_msgs, "D", 0)

    assert counts["茅台"] == 1
    assert counts["_other"] == 1


def test_merge_results_skipped_count(app: App) -> None:
    """skipped_count 透传到 _skipped。"""
    from src.plugins.word_pulse.analysis import merge_results
    counts, _ = merge_results([], [], {}, "D", skipped_count=7)
    assert counts["_skipped"] == 7
    assert counts["_other"] == 0


def test_merge_results_respects_max_sample(app: App) -> None:
    """cluster 命中数超过 max_sample 时,samples 按 max_sample 截断。"""
    from src.plugins.word_pulse.analysis import merge_results
    # 同一 cluster 命中 5 次
    all_msgs = {i: _mock_msg(i, et=i * 10) for i in range(1, 6)}
    direct = [(i, ["茅台"]) for i in range(1, 6)]
    counts, samples = merge_results(direct, [], all_msgs, "D", 0, max_sample=2)
    # counts 累计 5 条
    assert counts["茅台"] == 5
    # samples 最多 2 条(对应 max_sample)
    assert len(samples) == 2


def test_merge_results_missing_message_skipped(app: App) -> None:
    """direct 中 msg_id 不在 all_messages 时,计数仍累计但 sample 跳过(不 KeyError)。"""
    from src.plugins.word_pulse.analysis import merge_results
    direct = [(999, ["茅台"])]  # 999 不在 all_messages
    counts, samples = merge_results(direct, [], {}, "D", 0)
    assert counts["茅台"] == 1
    assert samples == []


# ═══════════════════════════════════════════════════════════════
# build_summary_prompt
# ═══════════════════════════════════════════════════════════════


def test_build_summary_prompt_with_samples(app: App) -> None:
    """buckets 带 samples 时,prompt 里包含「典型原文」段。"""
    from src.plugins.word_pulse.analysis import build_summary_prompt
    buckets = [
        {
            "day": "2026-01-01",
            "total_messages": 5,
            "counts": {"茅台": 3, "_other": 1, "_skipped": 1},
            "samples": [
                {"cluster": "茅台", "text": "茅台涨了", "author": "张三", "day": "2026-01-01"},
            ],
        },
    ]
    clusters = [{"name": "茅台"}, {"name": "五粮液"}]
    prompt = build_summary_prompt("炒股", clusters, buckets, "1天 (2026-01-01 ~ 2026-01-01)")
    assert "主题：炒股" in prompt
    assert "时间范围：1天" in prompt
    assert "- 茅台" in prompt
    assert "典型原文" in prompt
    assert "茅台涨了" in prompt


def test_build_summary_prompt_without_samples(app: App) -> None:
    """buckets 无 samples 时,prompt 不包含「典型原文」段。"""
    from src.plugins.word_pulse.analysis import build_summary_prompt
    buckets = [
        {
            "day": "2026-01-01",
            "total_messages": 0,
            "counts": {"_other": 0, "_skipped": 0},
            "samples": [],
        },
    ]
    clusters = [{"name": "茅台"}]
    prompt = build_summary_prompt("炒股", clusters, buckets, "1天")
    assert "主题：炒股" in prompt
    assert "典型原文" not in prompt


# ═══════════════════════════════════════════════════════════════
# _compute_day_bucket
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_compute_day_bucket_no_messages_saves_empty(app: App) -> None:
    """当天无消息时返回空 counts,并落库 sampled=False / total=0。"""
    from src.plugins.word_pulse.analysis import _compute_day_bucket
    from src.plugins.word_pulse.db import get_bucket, upsert_theme

    gid, theme_name = "88001", "炒股"
    tid = await upsert_theme(gid, theme_name)
    day_dt = datetime.now(SHANGHAI_TZ)

    bucket = await _compute_day_bucket(
        group_id=gid, theme_id=tid, theme_name=theme_name,
        day=day_dt.strftime("%Y-%m-%d"), day_dt=day_dt,
        clusters=[{"name": "茅台", "aliases": []}],
        char_pool={"茅"}, cluster_terms={"茅台": {"茅台"}},
        max_messages_per_bucket=100, max_sample_per_cluster=3,
        base_url="x", api_key="x", model="x", temperature=0.0, timeout=10.0,
    )
    assert bucket["total_messages"] == 0
    assert bucket["counts"] == {"_other": 0, "_skipped": 0}
    assert bucket["samples"] == []
    assert bucket["sampled"] is False

    # 落库可读回
    saved = await get_bucket(gid, tid, day_dt.strftime("%Y-%m-%d"))
    assert saved is not None
    assert saved["total_messages"] == 0
    assert saved["sampled"] == 0


@pytest.mark.asyncio
async def test_compute_day_bucket_with_messages_no_grey(app: App) -> None:
    """有消息且全部 direct 命中时,不调 LLM classify_batch,counts 正确。"""
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.word_pulse.analysis import _compute_day_bucket
    from src.plugins.word_pulse.db import upsert_theme
    from tests.test_word_pulse_analysis_extra import _make_group_event

    gid, theme_name = "88002", "炒股"
    tid = await upsert_theme(gid, theme_name)
    day_dt = datetime.now(SHANGHAI_TZ)
    day_start = int(datetime.combine(day_dt.date(), time.min, SHANGHAI_TZ).timestamp())

    # 灌 2 条 direct 命中的消息
    await archive_message_event(_make_group_event("茅台今天涨了", user_id=1, group_id=int(gid), message_id=1, event_time=day_start + 100))
    await archive_message_event(_make_group_event("茅台真好", user_id=2, group_id=int(gid), message_id=2, event_time=day_start + 200))

    # patch classify_batch 以确保它不会被调用(无 GREY)
    with patch(
        "src.plugins.word_pulse.analysis.classify_batch",
        new=AsyncMock(side_effect=AssertionError("不该调用 classify_batch")),
    ):
        bucket = await _compute_day_bucket(
            group_id=gid, theme_id=tid, theme_name=theme_name,
            day=day_dt.strftime("%Y-%m-%d"), day_dt=day_dt,
            clusters=[{"name": "茅台", "aliases": []}],
            char_pool={"茅"}, cluster_terms={"茅台": {"茅台"}},
            max_messages_per_bucket=100, max_sample_per_cluster=3,
            base_url="x", api_key="x", model="x", temperature=0.0, timeout=10.0,
        )
    assert bucket["total_messages"] == 2
    assert bucket["counts"]["茅台"] == 2
    assert bucket["sampled"] is False


@pytest.mark.asyncio
async def test_compute_day_bucket_with_grey_calls_classify(app: App) -> None:
    """有 GREY 区消息时调用 classify_batch,LLM 归类结果计入对应 cluster。"""
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.word_pulse.analysis import _compute_day_bucket
    from src.plugins.word_pulse.db import upsert_theme
    from tests.test_word_pulse_analysis_extra import _make_group_event

    gid, theme_name = "88003", "炒股"
    tid = await upsert_theme(gid, theme_name)
    day_dt = datetime.now(SHANGHAI_TZ)
    day_start = int(datetime.combine(day_dt.date(), time.min, SHANGHAI_TZ).timestamp())

    # 灌 1 条 GREY 消息:含 char_pool 字符但不含 cluster term
    await archive_message_event(_make_group_event("跌停了心态崩", user_id=1, group_id=int(gid), message_id=10, event_time=day_start + 100))

    fake_classify = AsyncMock(return_value=[(10, "茅台")])  # LLM 归到茅台
    with patch("src.plugins.word_pulse.analysis.classify_batch", new=fake_classify):
        bucket = await _compute_day_bucket(
            group_id=gid, theme_id=tid, theme_name=theme_name,
            day=day_dt.strftime("%Y-%m-%d"), day_dt=day_dt,
            clusters=[{"name": "茅台", "aliases": []}],
            char_pool={"跌", "茅"}, cluster_terms={"茅台": {"茅台"}},
            max_messages_per_bucket=100, max_sample_per_cluster=3,
            base_url="x", api_key="x", model="x", temperature=0.0, timeout=10.0,
        )
    assert bucket["total_messages"] == 1
    assert bucket["counts"]["茅台"] == 1
    fake_classify.assert_awaited_once()


@pytest.mark.asyncio
async def test_compute_day_bucket_samples_when_over_max(app: App) -> None:
    """消息数 > max_messages_per_bucket 时,sampled_flag 置 True 且调用 uniform_sample。"""
    from src.plugins.message_archive.db import archive_message_event
    from src.plugins.word_pulse.analysis import _compute_day_bucket
    from src.plugins.word_pulse.db import upsert_theme
    from tests.test_word_pulse_analysis_extra import _make_group_event

    gid, theme_name = "88004", "炒股"
    tid = await upsert_theme(gid, theme_name)
    day_dt = datetime.now(SHANGHAI_TZ)
    day_start = int(datetime.combine(day_dt.date(), time.min, SHANGHAI_TZ).timestamp())

    # 灌 5 条消息,但 max_messages_per_bucket=2
    for i in range(5):
        await archive_message_event(_make_group_event(
            f"茅台{i}", user_id=i, group_id=int(gid), message_id=20 + i, event_time=day_start + i * 100
        ))

    bucket = await _compute_day_bucket(
        group_id=gid, theme_id=tid, theme_name=theme_name,
        day=day_dt.strftime("%Y-%m-%d"), day_dt=day_dt,
        clusters=[{"name": "茅台", "aliases": []}],
        char_pool={"茅"}, cluster_terms={"茅台": {"茅台"}},
        max_messages_per_bucket=2, max_sample_per_cluster=3,
        base_url="x", api_key="x", model="x", temperature=0.0, timeout=10.0,
    )
    # 被截断到 max_messages_per_bucket=2
    assert bucket["total_messages"] == 2
    assert bucket["sampled"] is True


# ═══════════════════════════════════════════════════════════════
# compute_or_load_buckets
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_compute_or_load_buckets_history_day_uses_cache(app: App) -> None:
    """历史日(非今日)的桶只要缓存存在就直接复用,不重新计算。

    day_range=2 时,offset=0 是今日(无缓存会重算),offset=1 是昨日。
    我们给昨日预填缓存 → 断言 _compute_day_bucket 仅被调用 1 次(只算今日)。
    """
    from src.plugins.word_pulse.analysis import compute_or_load_buckets
    from src.plugins.word_pulse.db import save_bucket, upsert_theme

    gid, theme_name = "88005", "炒股"
    tid = await upsert_theme(gid, theme_name)

    # 灌一个昨天日期的桶缓存
    yesterday = (datetime.now(SHANGHAI_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    await save_bucket(
        gid, tid, yesterday,
        json.dumps({"茅台": 9, "_other": 0, "_skipped": 0}, ensure_ascii=False),
        "[]", 9, False,
    )

    # 今日无缓存 → 必须重算(允许调用 1 次);昨日有缓存 → 不该调用
    fake_compute = AsyncMock(return_value={
        "day": "X", "total_messages": 0,
        "counts": {"_other": 0, "_skipped": 0}, "samples": [], "sampled": False,
    })
    with patch("src.plugins.word_pulse.analysis._compute_day_bucket", new=fake_compute):
        buckets = await compute_or_load_buckets(
            group_id=gid, theme_id=tid, theme_name=theme_name,
            clusters=[{"name": "茅台", "aliases": []}],
            char_pool={"茅"}, cluster_terms={"茅台": {"茅台"}},
            day_range=2, base_url="x", api_key="x", model="x",
            max_messages_per_bucket=100, max_sample_per_cluster=3,
            temperature=0.0, timeout=10.0,
            today_bucket_fresh_seconds=300,
        )
    # 今日重算 1 次,昨日命中缓存不调用
    assert fake_compute.await_count == 1
    assert len(buckets) == 2
    # buckets 按日期升序: yesterday 在前
    assert buckets[0]["day"] == yesterday
    assert buckets[0]["counts"]["茅台"] == 9


@pytest.mark.asyncio
async def test_compute_or_load_buckets_today_fresh_uses_cache(app: App) -> None:
    """今日桶在 freshness 窗口内时复用缓存。"""
    from src.plugins.word_pulse.analysis import compute_or_load_buckets
    from src.plugins.word_pulse.db import save_bucket, upsert_theme

    gid, theme_name = "88006", "炒股"
    tid = await upsert_theme(gid, theme_name)
    today = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")

    # 灌一个今天刚算(computed_at = now)的桶
    await save_bucket(
        gid, tid, today,
        json.dumps({"茅台": 3, "_other": 0, "_skipped": 0}, ensure_ascii=False),
        "[]", 3, False,
    )
    # save_bucket 内部用 time.time() 作 computed_at,freshness=300 必然命中

    with patch(
        "src.plugins.word_pulse.analysis._compute_day_bucket",
        new=AsyncMock(side_effect=AssertionError("今日 freshness 内不该重算")),
    ):
        buckets = await compute_or_load_buckets(
            group_id=gid, theme_id=tid, theme_name=theme_name,
            clusters=[{"name": "茅台", "aliases": []}],
            char_pool={"茅"}, cluster_terms={"茅台": {"茅台"}},
            day_range=1, base_url="x", api_key="x", model="x",
            max_messages_per_bucket=100, max_sample_per_cluster=3,
            temperature=0.0, timeout=10.0,
            today_bucket_fresh_seconds=300,
        )
    assert len(buckets) == 1
    assert buckets[0]["counts"]["茅台"] == 3


@pytest.mark.asyncio
async def test_compute_or_load_buckets_today_stale_recomputes(app: App) -> None:
    """今日桶 freshness 过期后重新计算。"""
    from src.plugins.word_pulse.analysis import compute_or_load_buckets
    from src.plugins.word_pulse.db import get_db, upsert_theme

    gid, theme_name = "88007", "炒股"
    tid = await upsert_theme(gid, theme_name)
    today = datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d")

    # 灌一个 stale 缓存:computed_at = 0(1970),freshness=300 必过期
    db = get_db()
    counts_json = json.dumps({"_other": 0, "_skipped": 0}, ensure_ascii=False)
    await db.execute(
        """INSERT INTO word_pulse_bucket
           (group_id, theme_id, day, counts_json, samples_json, total_messages, sampled, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (gid, tid, today, counts_json, "[]", 0, 0, 0),
    )

    fake_compute = AsyncMock(return_value={
        "day": today, "total_messages": 0,
        "counts": {"_other": 0, "_skipped": 0}, "samples": [], "sampled": False,
    })
    with patch("src.plugins.word_pulse.analysis._compute_day_bucket", new=fake_compute):
        await compute_or_load_buckets(
            group_id=gid, theme_id=tid, theme_name=theme_name,
            clusters=[{"name": "茅台", "aliases": []}],
            char_pool={"茅"}, cluster_terms={"茅台": {"茅台"}},
            day_range=1, base_url="x", api_key="x", model="x",
            max_messages_per_bucket=100, max_sample_per_cluster=3,
            temperature=0.0, timeout=10.0,
            today_bucket_fresh_seconds=300,
        )
    fake_compute.assert_awaited_once()


@pytest.mark.asyncio
async def test_compute_or_load_buckets_no_cache_computes(app: App) -> None:
    """缓存完全不存在时调用 _compute_day_bucket 全新计算。"""
    from src.plugins.word_pulse.analysis import compute_or_load_buckets
    from src.plugins.word_pulse.db import upsert_theme

    gid, theme_name = "88008", "炒股"
    tid = await upsert_theme(gid, theme_name)

    fake_compute = AsyncMock(return_value={
        "day": "X", "total_messages": 0,
        "counts": {"_other": 0, "_skipped": 0}, "samples": [], "sampled": False,
    })
    with patch("src.plugins.word_pulse.analysis._compute_day_bucket", new=fake_compute):
        buckets = await compute_or_load_buckets(
            group_id=gid, theme_id=tid, theme_name=theme_name,
            clusters=[{"name": "茅台", "aliases": []}],
            char_pool={"茅"}, cluster_terms={"茅台": {"茅台"}},
            day_range=1, base_url="x", api_key="x", model="x",
            max_messages_per_bucket=100, max_sample_per_cluster=3,
            temperature=0.0, timeout=10.0,
            today_bucket_fresh_seconds=300,
        )
    fake_compute.assert_awaited_once()
    assert len(buckets) == 1


@pytest.mark.asyncio
async def test_compute_or_load_buckets_multi_day_reverses(app: App) -> None:
    """day_range>1 时,返回的 buckets 按日期升序(内部逆序)。"""
    from src.plugins.word_pulse.analysis import compute_or_load_buckets
    from src.plugins.word_pulse.db import upsert_theme

    gid, theme_name = "88009", "炒股"
    tid = await upsert_theme(gid, theme_name)

    # _compute_day_bucket 被调用 day_range 次,每次返回 day=offset
    async def fake_compute(*, day, **_):
        return {
            "day": day, "total_messages": 0,
            "counts": {"_other": 0, "_skipped": 0}, "samples": [], "sampled": False,
        }

    with patch("src.plugins.word_pulse.analysis._compute_day_bucket", new=AsyncMock(side_effect=fake_compute)):
        buckets = await compute_or_load_buckets(
            group_id=gid, theme_id=tid, theme_name=theme_name,
            clusters=[{"name": "茅台", "aliases": []}],
            char_pool={"茅"}, cluster_terms={"茅台": {"茅台"}},
            day_range=3, base_url="x", api_key="x", model="x",
            max_messages_per_bucket=100, max_sample_per_cluster=3,
            temperature=0.0, timeout=10.0,
            today_bucket_fresh_seconds=300,
        )
    # 3 天,按日期升序
    assert len(buckets) == 3
    days = [b["day"] for b in buckets]
    assert days == sorted(days)


# ═══════════════════════════════════════════════════════════════
# 工厂:GroupMessageEvent(用于 archive_message_event)
# ═══════════════════════════════════════════════════════════════


def _make_group_event(
    message, *, message_id=1, user_id=100001, group_id=200001,
    self_id=987654321, nickname="测试用户", card="", event_time=None,
):
    """模块级工厂,供 _compute_day_bucket 测试在内部 import 使用。"""
    from datetime import datetime as _dt

    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
    from nonebot.adapters.onebot.v11.event import Sender
    actual = message if isinstance(message, Message) else Message(message)
    return GroupMessageEvent(
        time=event_time or int(_dt.now().timestamp()),
        self_id=self_id, post_type="message", sub_type="normal",
        user_id=user_id, message_type="group", group_id=group_id,
        message_id=message_id, message=actual, original_message=actual.copy(),
        raw_message=str(actual), font=0,
        sender=Sender(user_id=user_id, nickname=nickname, card=card, role="member"),
    )
