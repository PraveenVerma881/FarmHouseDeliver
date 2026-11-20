from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg
from fastapi import Request


async def open_pool() -> asyncpg.Pool:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL must point to a PostgreSQL/PostGIS database")
    return await asyncpg.create_pool(
        dsn=database_url,
        min_size=1,
        max_size=int(os.environ.get("DB_POOL_MAX", "10")),
        command_timeout=8,
    )


async def get_connection(request: Request) -> AsyncIterator[asyncpg.Connection]:
    pool: asyncpg.Pool = request.app.state.db_pool
    async with pool.acquire() as connection:
        yield connection