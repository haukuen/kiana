# src/plugins/word_pulse/db.py
from __future__ import annotations

import json
import time

from src.storage import get_db

db = get_db()

SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS word_pulse_theme (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT NOT NULL,
        name TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE(group_id, name)
    )""",
    """CREATE TABLE IF NOT EXISTS word_pulse_cluster (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        theme_id INTEGER NOT NULL REFERENCES word_pulse_theme(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(theme_id, name)
    )""",
    """CREATE TABLE IF NOT EXISTS word_pulse_charset (
        cluster_id INTEGER PRIMARY KEY REFERENCES word_pulse_cluster(id) ON DELETE CASCADE,
        chars_json TEXT NOT NULL,
        expanded_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS word_pulse_bucket (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id TEXT NOT NULL,
        theme_id INTEGER NOT NULL REFERENCES word_pulse_theme(id) ON DELETE CASCADE,
        day TEXT NOT NULL,
        counts_json TEXT NOT NULL,
        samples_json TEXT NOT NULL,
        total_messages INTEGER NOT NULL,
        sampled INTEGER NOT NULL DEFAULT 0,
        computed_at INTEGER NOT NULL,
        UNIQUE(group_id, theme_id, day)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_word_pulse_theme_group ON word_pulse_theme(group_id)",
    "CREATE INDEX IF NOT EXISTS idx_word_pulse_cluster_theme ON word_pulse_cluster(theme_id)",
    "CREATE INDEX IF NOT EXISTS idx_word_pulse_bucket_lookup ON word_pulse_bucket(group_id, theme_id, day)",
]


def ensure_schema() -> None:
    db.ensure_schema(SCHEMA_STATEMENTS)


# ── Theme ──


async def upsert_theme(group_id: str, name: str) -> int:
    """创建或更新主题。返回 theme_id。"""
    now = int(time.time())
    row = await db.fetch_one(
        "SELECT id FROM word_pulse_theme WHERE group_id = ? AND name = ?",
        (group_id, name),
    )
    if row:
        await db.execute(
            "UPDATE word_pulse_theme SET updated_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        return row["id"]
    await db.execute(
        "INSERT INTO word_pulse_theme (group_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (group_id, name, now, now),
    )
    row = await db.fetch_one(
        "SELECT id FROM word_pulse_theme WHERE group_id = ? AND name = ?",
        (group_id, name),
    )
    assert row is not None
    return row["id"]


async def get_theme(group_id: str, name: str) -> dict | None:
    """按群+名称查找主题，返回行 dict 或 None。"""
    row = await db.fetch_one(
        "SELECT id, group_id, name, created_at, updated_at FROM word_pulse_theme WHERE group_id = ? AND name = ?",
        (group_id, name),
    )
    return None if row is None else dict(row)


async def list_themes(group_id: str) -> list[dict]:
    """列出指定群的所有主题。"""
    rows = await db.fetch_all(
        "SELECT id, group_id, name, created_at, updated_at FROM word_pulse_theme WHERE group_id = ? ORDER BY updated_at DESC",
        (group_id,),
    )
    return [dict(r) for r in rows]


async def delete_theme(theme_id: int) -> None:
    """删除主题（级联删除 cluster/charset/bucket）。"""
    await db.execute("DELETE FROM word_pulse_theme WHERE id = ?", (theme_id,))


# ── Cluster ──


async def replace_clusters(theme_id: int, names: list[str]) -> list[int]:
    old = await get_clusters(theme_id)
    old_names = {c["name"]: c["id"] for c in old}
    new_names = set(names)
    for c in old:
        if c["name"] not in new_names:
            await db.execute("DELETE FROM word_pulse_cluster WHERE id = ?", (c["id"],))
    now = int(time.time())
    result_ids: list[int] = []
    for name in names:
        if name in old_names:
            result_ids.append(old_names[name])
        else:
            await db.execute(
                "INSERT INTO word_pulse_cluster (theme_id, name, created_at) VALUES (?, ?, ?)",
                (theme_id, name, now),
            )
            row = await db.fetch_one("SELECT id FROM word_pulse_cluster WHERE theme_id = ? AND name = ?", (theme_id, name))
            assert row is not None
            result_ids.append(row["id"])
    return result_ids


async def add_clusters(theme_id: int, names: list[str]) -> list[int]:
    existing = await get_clusters(theme_id)
    existing_names = {c["name"] for c in existing}
    now = int(time.time())
    new_ids: list[int] = []
    for name in names:
        if name in existing_names:
            continue
        await db.execute(
            "INSERT INTO word_pulse_cluster (theme_id, name, created_at) VALUES (?, ?, ?)",
            (theme_id, name, now),
        )
        row = await db.fetch_one("SELECT id FROM word_pulse_cluster WHERE theme_id = ? AND name = ?", (theme_id, name))
        assert row is not None
        new_ids.append(row["id"])
        existing_names.add(name)
    return new_ids


async def get_clusters(theme_id: int) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT id, theme_id, name, created_at FROM word_pulse_cluster WHERE theme_id = ? ORDER BY id",
        (theme_id,),
    )
    return [dict(r) for r in rows]


async def delete_cluster(cluster_id: int) -> None:
    await db.execute("DELETE FROM word_pulse_cluster WHERE id = ?", (cluster_id,))


# ── Charset ──


async def save_charsets(theme_id: int, charset_map: dict[str, list[str]]) -> None:
    clusters = await get_clusters(theme_id)
    cluster_by_name = {c["name"]: c["id"] for c in clusters}
    now = int(time.time())
    for name, chars in charset_map.items():
        cid = cluster_by_name.get(name)
        if cid is None:
            continue
        await db.execute(
            """INSERT INTO word_pulse_charset (cluster_id, chars_json, expanded_at)
               VALUES (?, ?, ?)
               ON CONFLICT(cluster_id) DO UPDATE SET chars_json=excluded.chars_json, expanded_at=excluded.expanded_at""",
            (cid, json.dumps(chars, ensure_ascii=False), now),
        )


async def get_expanded_charset(theme_id: int) -> set[str]:
    rows = await db.fetch_all(
        """SELECT cs.chars_json FROM word_pulse_charset cs
           JOIN word_pulse_cluster cl ON cs.cluster_id = cl.id
           WHERE cl.theme_id = ?""",
        (theme_id,),
    )
    result: set[str] = set()
    for row in rows:
        result.update(json.loads(row["chars_json"]))
    return result


# ── Bucket ──


async def save_bucket(
    group_id: str, theme_id: int, day: str,
    counts_json: str, samples_json: str,
    total_messages: int, sampled: bool,
) -> None:
    now = int(time.time())
    sampled_int = 1 if sampled else 0
    await db.execute(
        """INSERT INTO word_pulse_bucket (group_id, theme_id, day, counts_json, samples_json, total_messages, sampled, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(group_id, theme_id, day) DO UPDATE SET
               counts_json=excluded.counts_json, samples_json=excluded.samples_json,
               total_messages=excluded.total_messages, sampled=excluded.sampled, computed_at=excluded.computed_at""",
        (group_id, theme_id, day, counts_json, samples_json, total_messages, sampled_int, now),
    )


async def get_bucket(group_id: str, theme_id: int, day: str) -> dict | None:
    row = await db.fetch_one(
        "SELECT id, group_id, theme_id, day, counts_json, samples_json, total_messages, sampled, computed_at "
        "FROM word_pulse_bucket WHERE group_id=? AND theme_id=? AND day=?",
        (group_id, theme_id, day),
    )
    return None if row is None else dict(row)


async def delete_buckets_by_theme(group_id: str, theme_id: int) -> None:
    await db.execute("DELETE FROM word_pulse_bucket WHERE group_id=? AND theme_id=?", (group_id, theme_id))


async def delete_buckets_older_than(before_day: str) -> int:
    row = await db.fetch_one("SELECT COUNT(*) AS cnt FROM word_pulse_bucket WHERE day < ?", (before_day,))
    before = row["cnt"] if row else 0
    await db.execute("DELETE FROM word_pulse_bucket WHERE day < ?", (before_day,))
    return before


async def get_bucket_days(group_id: str, theme_id: int) -> list[str]:
    rows = await db.fetch_all(
        "SELECT day FROM word_pulse_bucket WHERE group_id=? AND theme_id=? ORDER BY day",
        (group_id, theme_id),
    )
    return [r["day"] for r in rows]
