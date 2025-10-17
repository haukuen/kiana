"""Lightweight SQLite manager for shared bot storage."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence


class SQLiteManager:
    """Serialize SQLite access across async code paths."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")

        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._db_path

    def ensure_schema(self, statements: Iterable[str]) -> None:
        """Run schema creation statements synchronously."""
        with self._conn:
            for statement in statements:
                self._conn.execute(statement)

    async def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        await self._run(sql, params, fetch_kind=None)

    async def fetch_one(self, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Row | None:
        return await self._run(sql, params, fetch_kind="one")

    async def fetch_all(self, sql: str, params: Sequence[Any] | None = None) -> list[sqlite3.Row]:
        result = await self._run(sql, params, fetch_kind="all")
        return result if result is not None else []

    async def close(self) -> None:
        async with self._lock:
            self._conn.close()

    async def _run(self, sql: str, params: Sequence[Any] | None, fetch_kind: str | None) -> Any:
        args = tuple(params) if params is not None else ()
        loop = asyncio.get_running_loop()
        async with self._lock:
            return await loop.run_in_executor(None, self._execute_sync, sql, args, fetch_kind)

    def _execute_sync(self, sql: str, params: tuple[Any, ...], fetch_kind: str | None) -> Any:
        cursor = self._conn.execute(sql, params)
        result: Any
        if fetch_kind == "one":
            result = cursor.fetchone()
        elif fetch_kind == "all":
            result = cursor.fetchall()
        else:
            result = None
        self._conn.commit()
        return result


_DB_INSTANCE: SQLiteManager | None = None


def get_db() -> SQLiteManager:
    """Return the singleton SQLite manager instance."""

    global _DB_INSTANCE
    if _DB_INSTANCE is None:
        project_root = Path(__file__).resolve().parents[2]
        db_path = project_root / "data" / "kiana.sqlite3"
        _DB_INSTANCE = SQLiteManager(db_path)
    return _DB_INSTANCE
