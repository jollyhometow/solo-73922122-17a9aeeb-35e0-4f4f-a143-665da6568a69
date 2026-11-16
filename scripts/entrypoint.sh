#!/usr/bin/env bash
# 容器入口: 等待 PG -> 幂等迁移 -> 启动 uvicorn
set -euo pipefail

echo "[entrypoint] waiting for PostgreSQL..."
python - <<'PY'
import os, time, psycopg

url = os.environ["DATABASE_URL"]
deadline = time.time() + 60
last = None
while time.time() < deadline:
    try:
        with psycopg.connect(url, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        print("[entrypoint] database is ready")
        break
    except Exception as exc:  # pragma: no cover - 启动期重试
        last = exc
        time.sleep(1)
else:
    raise SystemExit(f"[entrypoint] database not ready: {last}")
PY

echo "[entrypoint] applying migrations..."
python scripts/migrate.py

echo "[entrypoint] starting API on :8000"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
