"""API 层测试: CRUD、校验、同快照供数。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _create(meter, ts, reading, replaced=False):
    return client.post(
        "/readings",
        json={
            "meter_no": meter,
            "read_at": ts,
            "reading": reading,
            "replaced": replaced,
        },
    )


def test_health(clean_db):
    assert client.get("/health").json() == {"status": "ok"}


def test_openapi_docs_available(clean_db):
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "/readings" in spec["paths"] and "/supply" in spec["paths"]


def test_full_flow_via_api(clean_db):
    r = _create("M-API", "2026-01-01T00:00:00Z", 1000)
    assert r.status_code == 201, r.text
    first = r.json()
    r = _create("M-API", "2026-01-01T01:00:00Z", 900)
    second = r.json()

    intervals = client.get("/intervals", params={"meter_no": "M-API"}).json()
    assert len(intervals) == 1
    assert intervals[0]["energy_wh"] == 999900  # 回绕

    # 更正
    r = client.put(
        f"/readings/{second['id']}",
        json={
            "meter_no": "M-API",
            "read_at": "2026-01-01T01:00:00Z",
            "reading": 1300,
            "replaced": False,
        },
    )
    assert r.status_code == 200, r.text
    intervals = client.get("/intervals", params={"meter_no": "M-API"}).json()
    assert intervals[0]["energy_wh"] == 300

    # 供数: 读数与区间同快照
    supply = client.get("/supply", params={"meter_no": "M-API"}).json()
    assert len(supply["readings"]) == 2
    assert len(supply["intervals"]) == 1
    assert supply["snapshot_time"]

    # 删除
    r = client.delete(f"/readings/{second['id']}")
    assert r.status_code == 200
    assert client.get("/intervals", params={"meter_no": "M-API"}).json() == []
    # 首条读数仍在
    assert len(client.get("/readings", params={"meter_no": "M-API"}).json()) == 1


def test_replacement_null_energy_via_api(clean_db):
    _create("M-R", "2026-01-01T00:00:00Z", 999500)
    _create("M-R", "2026-01-01T01:00:00Z", 200, replaced=True)
    intervals = client.get("/intervals", params={"meter_no": "M-R"}).json()
    assert intervals[0]["energy_wh"] is None


def test_conflict_and_validation_errors(clean_db):
    _create("M-E", "2026-01-01T00:00:00Z", 100)
    dup = _create("M-E", "2026-01-01T00:00:00Z", 200)
    assert dup.status_code == 409

    missing = client.put(
        "/readings/999999",
        json={
            "meter_no": "M-E",
            "read_at": "2026-01-01T00:00:00Z",
            "reading": 1,
            "replaced": False,
        },
    )
    assert missing.status_code == 404

    bad_range = _create("M-E", "2026-01-01T02:00:00Z", 1_000_000)
    assert bad_range.status_code == 422

    naive = _create("M-E", "2026-01-01T03:00:00", 100)
    assert naive.status_code == 422


def test_update_nonexistent_returns_404(clean_db):
    r = client.delete("/readings/123456")
    assert r.status_code == 404


def test_supply_uses_repeatable_read_snapshot(clean_db):
    """供数连接确实运行在 REPEATABLE READ, 保证读数与区间同快照。"""
    from app.db import db

    with db.snapshot_connection() as conn:
        level = conn.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]
        assert level.lower() == "repeatable read"
