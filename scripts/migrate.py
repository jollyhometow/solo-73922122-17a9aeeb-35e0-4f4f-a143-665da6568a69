"""幂等迁移: 按文件名顺序执行 sql/ 下全部脚本。

DDL 全部使用 IF NOT EXISTS / CREATE OR REPLACE, 可重复执行;
每次迁移在单事务内完成, 失败回滚不残留半套结构。
"""
from __future__ import annotations

import os
import pathlib
import sys

import psycopg

SQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "sql"


def main() -> None:
    url = os.environ.get(
        "DATABASE_URL", "postgresql://postgres:postgres@db:5432/meterdb"
    )
    scripts = sorted(SQL_DIR.glob("*.sql"))
    if not scripts:
        raise SystemExit(f"no SQL files under {SQL_DIR}")

    with psycopg.connect(url) as conn:
        conn.autocommit = False
        try:
            for path in scripts:
                print(f"[migrate] applying {path.name} ...", flush=True)
                conn.execute(path.read_text(encoding="utf-8"))
            conn.commit()
        except Exception:
            conn.rollback()
            print("[migrate] FAILED, transaction rolled back", file=sys.stderr)
            raise
    print("[migrate] done")


if __name__ == "__main__":
    main()
