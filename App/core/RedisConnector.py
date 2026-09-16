# App/core/RedisConnector.py
import asyncio
import redis.asyncio as redis
from redis.exceptions import RedisError
from App.core.settings import settings
from App.core.LoggingInit import get_core_logger

logger = get_core_logger(__name__)


class RedisClient:
    def __init__(self):
        self._client: redis.Redis | None = None
        self._is_connected: bool = False
        self._connect_lock: asyncio.Lock = asyncio.Lock()

    async def _connect_unlocked(self) -> None:
        """
        Connect body with no locking. Caller must hold _connect_lock.

        Split out so ensure_connected() can hold the lock and run this
        without deadlocking on connect()'s own lock acquisition.
        """
        try:
            self._client = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                max_connections=50,
            )
            await self._client.ping()
            self._is_connected = True
            logger.info("Redis connected")
        except (RedisError, OSError, ConnectionError, TimeoutError) as e:
            if self._client is not None:
                try:
                    await self._client.aclose()
                except Exception:
                    pass
            self._client = None
            self._is_connected = False
            logger.error(f"Redis connection failed: {e}")
            raise RuntimeError("Redis unavailable") from e

    async def connect(self) -> None:
        """Connect (or no-op if already connected). Serialized by lock."""
        async with self._connect_lock:
            # Another coroutine may have connected while we waited on the lock.
            if self._client is not None and self._is_connected:
                return
            await self._connect_unlocked()

    async def disconnect(self) -> None:
        async with self._connect_lock:
            if self._client:
                await self._client.aclose()
                self._client = None
                self._is_connected = False
                logger.info("Redis disconnected")

    async def health_check(self) -> dict:
        try:
            if not self._client:
                return {"status": "disconnected", "connected": False}
            await self._client.ping()
            return {"status": "healthy", "connected": True}
        except Exception as e:
            return {"status": "error", "connected": False, "error": str(e)}

    @property
    def client(self) -> redis.Redis:
        if not self._client or not self._is_connected:
            raise RuntimeError("Redis not connected. Call connect() first.")
        return self._client

    async def ensure_connected(self) -> redis.Redis:
        """
        Return a live client, reconnecting if needed.

        Double-checked pattern: fast path when already connected, then
        serialize reconnect through the lock so N concurrent callers
        produce ONE new client, not N.
        """
        if self._client is not None and self._is_connected:
            return self._client

        async with self._connect_lock:
            # Re-check inside the lock: another coroutine may have
            # reconnected while we were waiting.
            if self._client is None or not self._is_connected:
                await self._connect_unlocked()
            return self._client


redis_client = RedisClient()


async def get_redis() -> redis.Redis:
    """
    FastAPI dependency.

    Calls ensure_connected() so a Redis blip reconnects instead of
    raising, which would 500 every request that touches Redis.
    """
    return await redis_client.ensure_connected()