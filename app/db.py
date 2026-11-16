"""PostgreSQL 同步连接池 (psycopg 3)。

写入与供数都在单个事务内完成, 供数显式使用 REPEATABLE READ,
保证一次请求里读数与区间来自同一个提交快照。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
import psycopg_pool
from psycopg.rows import dict_row

from .config import settings


class Database:
    def __init__(self) -> None:
        self._pool: psycopg_pool.ConnectionPool | None = None

    def connect(self) -> None:
        if self._pool is None:
            self._pool = psycopg_pool.ConnectionPool(
                settings.database_url,
                min_size=settings.pool_min_size,
                max_size=settings.pool_max_size,
                kwargs={"row_factory": dict_row, "autocommit": False},
                open=True,
            )
    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        """普通读写事务连接 (READ COMMITTED, PostgreSQL 默认级别)。"""
        assert self._pool is not None, "Database.connect() 未调用"
        with self._pool.connection() as conn:
            # 池化连接可能残留事务状态, 显式保证干净起点
            conn.rollback()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @contextmanager
    def snapshot_connection(self) -> Iterator[psycopg.Connection]:
        """供数专用: REPEATABLE READ, 读数与区间取自同一完整提交快照。"""
        assert self._pool is not None, "Database.connect() 未调用"
        with self._pool.connection() as conn:
            conn.rollback()
            # REPEATABLE READ 事务内的所有 SELECT 共享同一快照
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise


db = Database()
