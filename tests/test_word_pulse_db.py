"""词频插件 db 层测试。

覆盖 cluster 的 alias 增删查、aliases 在 get_clusters 中的返回。
"""

from __future__ import annotations

import pytest
from nonebug import App


@pytest.fixture
async def word_pulse_db(app: App):
    """初始化 word_pulse schema 并返回 db 模块。"""
    from src.plugins.word_pulse import db as wp_db
    wp_db.ensure_schema()
    return wp_db


def _theme_id_seed(wp_db, gid: str = "g1", name: str = "炒股") -> int:
    """同步辅助：用 asyncio.run 创建 theme（仅测试）。"""
    import asyncio
    return asyncio.run(wp_db.upsert_theme(gid, name))


@pytest.mark.asyncio
async def test_get_clusters_returns_empty_aliases_by_default(word_pulse_db) -> None:
    wp_db = word_pulse_db
    tid = await wp_db.upsert_theme("g1", "炒股")
    await wp_db.replace_clusters(tid, ["茅台", "五粮液"])
    clusters = await wp_db.get_clusters(tid)
    assert len(clusters) == 2
    for c in clusters:
        assert c["aliases"] == []


@pytest.mark.asyncio
async def test_add_cluster_aliases_persists(word_pulse_db) -> None:
    wp_db = word_pulse_db
    tid = await wp_db.upsert_theme("g1", "炒股")
    await wp_db.replace_clusters(tid, ["茅台"])
    cluster = (await wp_db.get_clusters(tid))[0]

    await wp_db.add_cluster_aliases(cluster["id"], ["茅子", "飞天"])

    refreshed = (await wp_db.get_clusters(tid))[0]
    assert refreshed["aliases"] == ["茅子", "飞天"]


@pytest.mark.asyncio
async def test_add_cluster_aliases_idempotent(word_pulse_db) -> None:
    """重复添加同一别名不应重复。"""
    wp_db = word_pulse_db
    tid = await wp_db.upsert_theme("g1", "炒股")
    await wp_db.replace_clusters(tid, ["茅台"])
    cid = (await wp_db.get_clusters(tid))[0]["id"]

    await wp_db.add_cluster_aliases(cid, ["茅子", "飞天"])
    await wp_db.add_cluster_aliases(cid, ["飞天", "茅茅子"])  # 飞天 已存在

    aliases = (await wp_db.get_clusters(tid))[0]["aliases"]
    assert sorted(aliases) == ["茅子", "茅茅子", "飞天"]


@pytest.mark.asyncio
async def test_add_cluster_aliases_does_not_touch_cluster_name(word_pulse_db) -> None:
    """别名不能与任何现有 cluster name 冲突（防止别名变独立 cluster）。"""
    wp_db = word_pulse_db
    tid = await wp_db.upsert_theme("g1", "炒股")
    await wp_db.replace_clusters(tid, ["茅台", "五粮液"])
    maotai = next(c for c in await wp_db.get_clusters(tid) if c["name"] == "茅台")

    # 试图把"五粮液"加成"茅台"的别名 —— 应该被拒绝
    added = await wp_db.add_cluster_aliases(maotai["id"], ["五粮液"])
    assert added == []  # 无效别名被过滤

    refreshed = (await wp_db.get_clusters(tid))
    maotai_after = next(c for c in refreshed if c["name"] == "茅台")
    assert maotai_after["aliases"] == []


@pytest.mark.asyncio
async def test_remove_cluster_aliases(word_pulse_db) -> None:
    wp_db = word_pulse_db
    tid = await wp_db.upsert_theme("g1", "炒股")
    await wp_db.replace_clusters(tid, ["茅台"])
    cid = (await wp_db.get_clusters(tid))[0]["id"]

    await wp_db.add_cluster_aliases(cid, ["茅子", "飞天", "茅茅子"])
    removed = await wp_db.remove_cluster_aliases(cid, ["茅子", "飞天"])

    assert sorted(removed) == ["茅子", "飞天"]
    remaining = (await wp_db.get_clusters(tid))[0]["aliases"]
    assert remaining == ["茅茅子"]


@pytest.mark.asyncio
async def test_remove_cluster_aliases_nonexistent_silently_ignored(word_pulse_db) -> None:
    """删除不存在的别名不应报错，返回实际删除的（空）。"""
    wp_db = word_pulse_db
    tid = await wp_db.upsert_theme("g1", "炒股")
    await wp_db.replace_clusters(tid, ["茅台"])
    cid = (await wp_db.get_clusters(tid))[0]["id"]

    await wp_db.add_cluster_aliases(cid, ["茅子"])
    removed = await wp_db.remove_cluster_aliases(cid, ["不存在的别名"])
    assert removed == []
    assert (await wp_db.get_clusters(tid))[0]["aliases"] == ["茅子"]


@pytest.mark.asyncio
async def test_delete_cluster_cascades_aliases(word_pulse_db) -> None:
    """删除 cluster 时别名也应一并清掉（虽然 cluster 没了，主要测不报错）。"""
    wp_db = word_pulse_db
    tid = await wp_db.upsert_theme("g1", "炒股")
    await wp_db.replace_clusters(tid, ["茅台"])
    cid = (await wp_db.get_clusters(tid))[0]["id"]
    await wp_db.add_cluster_aliases(cid, ["茅子"])

    await wp_db.delete_cluster(cid)

    assert await wp_db.get_clusters(tid) == []
