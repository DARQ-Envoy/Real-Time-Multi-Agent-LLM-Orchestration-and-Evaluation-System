from __future__ import annotations

from urllib.parse import quote, urlparse, urlunparse

import asyncpg

from .settings import settings


async def create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=settings.DATABASE_URL,
        min_size=1,
        max_size=10,
        command_timeout=30,
    )


def _ro_dsn() -> str:
    parts = urlparse(settings.DATABASE_URL)
    netloc = (
        f"mega_ro:{quote(settings.MEGA_RO_PASSWORD, safe='')}"
        f"@{parts.hostname or 'db'}"
    )
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunparse(parts._replace(netloc=netloc))


async def create_ro_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=_ro_dsn(),
        min_size=1,
        max_size=4,
        command_timeout=30,
    )
