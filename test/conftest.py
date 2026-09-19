"""
Test fixtures — uses the REAL App.core.Connector.database.

Every test gets a session from the same engine your app uses,
wrapped in a transaction that is rolled back after the test.
"""
import asyncio
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from App.core.Connector import database, Base
from App.api.databases import MigrateTable  # noqa: F401  — register all models on Base


# ---------------------------------------------------------------------
# Session-level setup: connect + create schema ONCE
# ---------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session", autouse=True, loop_scope="session")
async def _setup_database():
    await database.connect()
    engine = database.engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await database.disconnect()


@pytest_asyncio.fixture(loop_scope="session")
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with database._session_factory() as session:
        async with session.begin():
            yield session
            await session.rollback()