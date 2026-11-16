"""应用配置: 全部来自环境变量, 提供容器内可用的默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:postgres@db:5432/meterdb",
    )
    # 连接池上下限
    pool_min_size: int = int(os.getenv("POOL_MIN_SIZE", "2"))
    pool_max_size: int = int(os.getenv("POOL_MAX_SIZE", "10"))
    # 供数查询默认窗口上限 (条), 防止误拉全表
    default_limit: int = int(os.getenv("DEFAULT_LIMIT", "1000"))
    max_limit: int = int(os.getenv("MAX_LIMIT", "10000"))


settings = Settings()
