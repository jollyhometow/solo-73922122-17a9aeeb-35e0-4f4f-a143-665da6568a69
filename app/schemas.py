"""Pydantic 请求/响应模型。"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

_METER_NO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]{0,63}$")


def _validate_meter_no(v: str) -> str:
    v = v.strip()
    if not _METER_NO_RE.match(v):
        raise ValueError(
            "meter_no 须为 1-64 位字母/数字及 _.-/ 且以字母或数字开头"
        )
    return v


def _to_utc(v: datetime) -> datetime:
    """统一转 UTC 存储; 不接受无时区时间, 避免时刻歧义。"""
    if v.tzinfo is None:
        raise ValueError("read_at 必须带时区偏移 (建议直接用 UTC, 如 2026-01-01T00:00:00Z)")
    return v.astimezone(timezone.utc)


class ReadingCreate(BaseModel):
    meter_no: str = Field(..., examples=["M-001"])
    read_at: datetime = Field(..., examples=["2026-01-01T00:00:00Z"])
    reading: int = Field(..., ge=0, le=999999, description="累计读数, 整数 Wh, 0~999999")
    replaced: bool = Field(False, description="换表标记: 该读数为新表首读")

    _v_meter = field_validator("meter_no")(_validate_meter_no)
    _v_time = field_validator("read_at")(_to_utc)


class ReadingUpdate(BaseModel):
    meter_no: str = Field(..., examples=["M-001"])
    read_at: datetime = Field(..., examples=["2026-01-01T00:00:00Z"])
    reading: int = Field(..., ge=0, le=999999)
    replaced: bool = False

    _v_meter = field_validator("meter_no")(_validate_meter_no)
    _v_time = field_validator("read_at")(_to_utc)


class ReadingOut(BaseModel):
    id: int
    meter_no: str
    read_at: datetime
    reading: int
    replaced: bool


class IntervalOut(BaseModel):
    meter_no: str
    start_read_at: datetime
    end_read_at: datetime
    start_reading: int | None
    end_reading: int
    energy_wh: int | None = Field(None, description="区间电量 Wh; null=换表缺测")


class SupplyResponse(BaseModel):
    """同一完整提交快照下的读数与区间供数结果。"""

    meter_no: str
    snapshot_time: datetime = Field(..., description="本次供数使用的事务快照时间 (UTC)")
    readings: list[ReadingOut]
    intervals: list[IntervalOut]
