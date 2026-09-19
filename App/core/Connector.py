# App/core/Connector.py

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, AsyncEngine
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from typing import AsyncGenerator, Optional, Dict, Any
from contextlib import asynccontextmanager
import logging

from tenacity import retry, stop_after_attempt, wait_exponential

from App.core.settings import settings
from App.core.exceptions import InfrastructureError


logger = logging.getLogger(__name__)


class Database:
    """Async PostgreSQL connector.

    Exception policy:
        - Connection failures raise `InfrastructureError` (a subclass of
          `DomainError`). main.py maps it to a 503 with a consistent shape.
        - Session-level failures (OperationalError, SQLAlchemyError) are
          rolled back and re-raised untouched. main.py's SQLAlchemy
          handlers translate them.
        - Business exceptions (HTTPException, DomainError, ...) roll back
          the transaction but pass through unchanged.
    """

    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url or settings.database_url
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker] = None
        self._is_connected = False
        self._connection_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def connect(self) -> None:
        """Connect to the database with retry/backoff."""
        if self._is_connected:
            return

        try:
            # Redact the password before logging.
            safe_url = self.db_url
            if "@" in safe_url:
                parts = safe_url.split("@")
                if ":" in parts[0]:
                    user_pass = parts[0].split(":")
                    if len(user_pass) > 1:
                        safe_url = f"{user_pass[0]}:****@{parts[1]}"
            logger.info(f"Connecting to database: {safe_url}")

            self._engine = create_async_engine(
                self.db_url,
                echo=getattr(settings, "DATABASE_ECHO", False),
                poolclass=AsyncAdaptedQueuePool,
                pool_size=settings.DATABASE_POOL_SIZE,
                max_overflow=settings.DATABASE_MAX_OVERFLOW,
                pool_recycle=settings.DATABASE_POOL_RECYCLE,
                pool_timeout=settings.DATABASE_POOL_TIMEOUT,
                pool_pre_ping=True,
                connect_args={
                    "server_settings": {
                        "search_path": getattr(settings, "DATABASE_SCHEMA", "public"),
                        "application_name": getattr(settings, "DATABASE_NAME", "fastapi_app"),
                        "timezone": "UTC",
                    },
                    "command_timeout": getattr(settings, "DATABASE_CONNECT_TIMEOUT", 30),
                },
                future=True,
                execution_options={"isolation_level": "READ COMMITTED"},
            )

            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                logger.debug("Database connection test successful")

            self._session_factory = async_sessionmaker(
                bind=self._engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autocommit=False,
                autoflush=False,
            )
            self._is_connected = True
            self._connection_error = None
            logger.info("Database connected successfully")

        except OperationalError as e:
            self._is_connected = False
            self._connection_error = str(e)
            logger.error(f"Database connection error: {e}")
            raise

        except Exception as e:
            self._is_connected = False
            self._connection_error = str(e)
            logger.error(f"Database error: {e}")
            raise InfrastructureError(f"Database connection failed: {e}") from e

    async def disconnect(self) -> None:
        """Dispose the engine and reset state."""
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            self._is_connected = False
            self._connection_error = None
            logger.info("Database disconnected")

    # ------------------------------------------------------------------
    # Health / introspection
    # ------------------------------------------------------------------
    async def health_check(self) -> Dict[str, Any]:
        """Return a status dict for the DB."""
        if not self._is_connected:
            return {
                "status": "disconnected",
                "connected": False,
                "error": self._connection_error or "Not connected",
            }

        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return {"status": "healthy", "connected": True, "error": None}
        except Exception as e:
            self._is_connected = False
            self._connection_error = str(e)
            logger.warning(f"Database health check failed: {e}")
            return {"status": "error", "connected": False, "error": str(e)}

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def connection_error(self) -> Optional[str]:
        return self._connection_error

    @property
    def engine(self) -> AsyncEngine:
        if not self._engine or not self._is_connected:
            raise InfrastructureError("Database not connected")
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker:
        if not self._session_factory or not self._is_connected:
            raise InfrastructureError("Database not connected")
        return self._session_factory

    # ------------------------------------------------------------------
    # Session context managers
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Yield a session inside a transaction.

        Commits on success. Rolls back on any exception. Only genuine
        SQLAlchemy failures are logged as DB errors — business exceptions
        (HTTPException, DomainError, ValueError, ...) roll back silently
        and propagate unchanged.
        """
        if not self._is_connected:
            logger.warning("Database not connected, attempting to reconnect...")
            try:
                await self.connect()
            except InfrastructureError:
                raise
            except Exception as e:
                logger.error(f"Failed to reconnect: {e}")
                raise InfrastructureError("Database unavailable") from e

        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except OperationalError as e:
                # Genuine connection-level failure.
                await session.rollback()
                self._is_connected = False
                self._connection_error = str(e)
                logger.error(f"Database operational error: {e}")
                raise
            except SQLAlchemyError as e:
                # Genuine SQL-level failure (constraint violation, bad query, ...).
                await session.rollback()
                logger.error(f"Database SQL error: {e}")
                raise
            except Exception:
                # Business exceptions and anything else. Roll back so we
                # don't half-commit, but do NOT relabel the exception.
                await session.rollback()
                raise
            finally:
                await session.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[AsyncSession, None]:
        """Session wrapped in an explicit transaction block."""
        async with self.session() as session:
            async with session.begin():
                yield session


# SQLAlchemy Base for models
Base = declarative_base()

# Application-wide singleton
database = Database()


# =====================================================================
# FastAPI dependency
# =====================================================================
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields a database session.

    Infrastructure failures propagate to main.py's exception handlers,
    which return a consistent 503 shape (with request_id and Retry-After)
    on every endpoint. Route-level HTTPExceptions pass through untouched.

    No local try/except — catching here would convert the exception
    class before the global handlers could match on it.
    """
    if not database.is_connected:
        await database.connect()   # raises InfrastructureError on failure

    async with database.session() as session:
        yield session


# =====================================================================
# Repository base
# =====================================================================
class BaseRepository:
    """Base for repositories — provides session and raw query helper."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def execute_raw(self, query: str, params: dict = None):
        return await self.session.execute(text(query), params or {})