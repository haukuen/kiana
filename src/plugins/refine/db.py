"""炼化插件持久层。

数据模型（v2 — 懒重炼）:
- ``refine_subscription``: 一个群内对某个目标的订阅（单用户或 un_nickname 集合）
- ``refine_result``: 与订阅 1:1 关系，每订阅最多 1 条；新结果 INSERT OR REPLACE
  旧结果，无需清理 cron

依赖 ``message_archive`` 表（由 ``src.plugins.message_archive.db.ensure_schema``
创建）作为发言数据源；依赖 ``nickname_collections`` 表（由 ``un_nickname`` 创建）
作为集合成员查询源。本插件 schema 只负责自己的两张表，跨插件只读不写。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Literal

from src.storage import get_db

TargetType = Literal["user", "collection"]


@dataclass(slots=True)
class RefineSubscription:
    id: int
    group_id: str
    target_type: TargetType
    target_value: str
    label: str
    created_at: int


@dataclass(slots=True)
class RefineResult:
    subscription_id: int
    period_start: int
    period_end: int
    summary: str
    message_count: int
    model_name: str
    created_at: int


_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS refine_subscription (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id     TEXT NOT NULL,
        target_type  TEXT NOT NULL,
        target_value TEXT NOT NULL,
        label        TEXT NOT NULL,
        created_at   INTEGER NOT NULL,
        UNIQUE (group_id, target_type, target_value),
        UNIQUE (group_id, label)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_refine_subscription_group
    ON refine_subscription (group_id)
    """,
    # v2: refine_result 与 subscription 1:1 关系，subscription_id 直接作主键。
    # 新结果用 INSERT OR REPLACE 原子替换旧结果，不需要清理 cron。
    """
    CREATE TABLE IF NOT EXISTS refine_result (
        subscription_id INTEGER PRIMARY KEY,
        period_start    INTEGER NOT NULL,
        period_end      INTEGER NOT NULL,
        summary         TEXT NOT NULL,
        message_count   INTEGER NOT NULL,
        model_name      TEXT NOT NULL,
        created_at      INTEGER NOT NULL,
        FOREIGN KEY (subscription_id) REFERENCES refine_subscription(id) ON DELETE CASCADE
    )
    """,
]


def ensure_schema() -> None:
    """确保炼化插件表结构存在。"""
    db = get_db()
    db.ensure_schema(_SCHEMA_STATEMENTS)


# ── row 映射 ──────────────────────────────────────────


def _row_to_subscription(row: sqlite3.Row) -> RefineSubscription:
    return RefineSubscription(
        id=row["id"],
        group_id=row["group_id"],
        target_type=row["target_type"],
        target_value=row["target_value"],
        label=row["label"],
        created_at=row["created_at"],
    )


def _row_to_result(row: sqlite3.Row) -> RefineResult:
    return RefineResult(
        subscription_id=row["subscription_id"],
        period_start=row["period_start"],
        period_end=row["period_end"],
        summary=row["summary"],
        message_count=row["message_count"],
        model_name=row["model_name"],
        created_at=row["created_at"],
    )


# ── 订阅 CRUD ──────────────────────────────────────────


async def add_subscription(
    *,
    group_id: str,
    target_type: TargetType,
    target_value: str,
    label: str,
) -> RefineSubscription | None:
    """新增订阅。返回新对象；若 (group_id, target_type, target_value) 或 label
    已存在则返回 None（由调用方区分两种冲突）。"""
    db = get_db()
    now = int(time.time())
    try:
        await db.execute(
            """
            INSERT INTO refine_subscription
                (group_id, target_type, target_value, label, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (group_id, target_type, target_value, label, now),
        )
    except sqlite3.IntegrityError:
        return None
    row = await db.fetch_one(
        "SELECT * FROM refine_subscription WHERE group_id = ? AND label = ?",
        (group_id, label),
    )
    return _row_to_subscription(row) if row else None


async def conflict_on_target(
    group_id: str, target_type: TargetType, target_value: str
) -> RefineSubscription | None:
    """检查 (group_id, target_type, target_value) 是否已被订阅。返回占用项或 None。"""
    db = get_db()
    row = await db.fetch_one(
        """
        SELECT * FROM refine_subscription
        WHERE group_id = ? AND target_type = ? AND target_value = ?
        """,
        (group_id, target_type, target_value),
    )
    return _row_to_subscription(row) if row else None


async def conflict_on_label(group_id: str, label: str) -> RefineSubscription | None:
    """检查 label 是否在该群已被使用。"""
    db = get_db()
    row = await db.fetch_one(
        "SELECT * FROM refine_subscription WHERE group_id = ? AND label = ?",
        (group_id, label),
    )
    return _row_to_subscription(row) if row else None


async def list_subscriptions(group_id: str) -> list[RefineSubscription]:
    db = get_db()
    rows = await db.fetch_all(
        """
        SELECT * FROM refine_subscription
        WHERE group_id = ?
        ORDER BY created_at ASC
        """,
        (group_id,),
    )
    return [_row_to_subscription(r) for r in rows]


async def get_subscription_by_label(
    group_id: str, label: str
) -> RefineSubscription | None:
    db = get_db()
    row = await db.fetch_one(
        "SELECT * FROM refine_subscription WHERE group_id = ? AND label = ?",
        (group_id, label),
    )
    return _row_to_subscription(row) if row else None


async def delete_subscription(*, group_id: str, label: str) -> bool:
    """删除订阅。ON DELETE CASCADE 会同时清理对应的 refine_result。"""
    db = get_db()
    existing = await get_subscription_by_label(group_id, label)
    if existing is None:
        return False
    await db.execute(
        "DELETE FROM refine_subscription WHERE id = ?",
        (existing.id,),
    )
    return True


# ── 结果 CRUD（1:1，无清理）────────────────────────────


async def save_result(
    *,
    subscription_id: int,
    period_start: int,
    period_end: int,
    summary: str,
    message_count: int,
    model_name: str,
) -> RefineResult:
    """保存炼化结果。与订阅 1:1，INSERT OR REPLACE 原子覆盖旧记录。"""
    db = get_db()
    now = int(time.time())
    await db.execute(
        """
        INSERT OR REPLACE INTO refine_result
            (subscription_id, period_start, period_end, summary,
             message_count, model_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (subscription_id, period_start, period_end, summary, message_count, model_name, now),
    )
    return RefineResult(
        subscription_id=subscription_id,
        period_start=period_start,
        period_end=period_end,
        summary=summary,
        message_count=message_count,
        model_name=model_name,
        created_at=now,
    )


async def get_result(subscription_id: int) -> RefineResult | None:
    """获取订阅的最新（且唯一）结果。"""
    db = get_db()
    row = await db.fetch_one(
        "SELECT * FROM refine_result WHERE subscription_id = ?",
        (subscription_id,),
    )
    return _row_to_result(row) if row else None
