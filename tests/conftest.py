"""测试夹具: 指向外部提供的 PostgreSQL (本地嵌入式 PG / Compose PG 均可)。

运行前需设置 DATABASE_URL, 每个测试模块重建干净的 schema。
"""
from __future__ import annotations

import os
import pathlib

import psycopg
import pytest

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres@127.0.0.1:55437/meterdb_t2"
)

from app.config import settings  # noqa: E402
from app.db import db  # noqa: E402

SQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "sql"


def _reset_database() -> None:
    """在固定测试库内重建 schema (避免沙箱下 DROP DATABASE 的信号权限问题)。"""
    admin_url = settings.database_url.rsplit("/", 1)[0] + "/postgres"
    db_name = settings.database_url.rsplit("/", 1)[1]
    with psycopg.connect(admin_url, autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
        ).fetchone()
        if exists is None:
            admin.execute(f"CREATE DATABASE {db_name}")
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        for path in sorted(SQL_DIR.glob("*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))
        conn.commit()


@pytest.fixture(scope="session", autouse=True)
def _database():
    _reset_database()
    db.connect()
    yield
    db.close()


@pytest.fixture()
def clean_db():
    """每个用例清空业务数据 (结构保留)。"""
    with db.connection() as conn:
        conn.execute("TRUNCATE reading_intervals, meter_readings, meters RESTART IDENTITY")
    yield
