# App/core/RedisConnector.py
import asyncio
import redis.asyncio as redis
from redis.exceptions import RedisError
from App.core.settings import settings
from App.core.LoggingInit import get_core_logger
from App.core.exceptions import InfrastructureError
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
            raise InfrastructureError("Redis unavailable") from e

    async def connect(self) -> None:
        """Connect (or no-op if already connected). Serialized by lock."""
        async with self._connect_lock:
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

    async def ensure_connected(self, retries: int = 1) -> redis.Redis:
        """
        Return a live, VERIFIED Redis client — reconnecting if needed.

        Unlike a plain flag check, this PINGS the cached client before
        handing it back, every time. A Redis that died silently after a
        previous successful connect is caught right here, at the point
        of use, and raises a clean InfrastructureError instead of handing
        callers a dead client that fails unpredictably somewhere downstream.

        retries: number of reconnect attempts after the first failure
        before giving up and raising. Each retry has a short backoff.
        """
        async with self._connect_lock:
            last_error: Exception | None = None

            for attempt in range(retries + 1):
                # Case 1: we believe we're connected — verify for real.
                if self._client is not None and self._is_connected:
                    try:
                        await self._client.ping()
                        return self._client
                    except (RedisError, OSError, ConnectionError, TimeoutError) as e:
                        logger.warning(
                            f"Redis ping failed on cached client "
                            f"(attempt {attempt + 1}/{retries + 1}): {e}"
                        )
                        last_error = e
                        self._is_connected = False
                        try:
                            await self._client.aclose()
                        except Exception:
                            pass
                        self._client = None

                # Case 2: not connected (or just detected dead) — (re)connect.
                try:
                    await self._connect_unlocked()
                    return self._client
                except InfrastructureError as e:
                    last_error = e
                    if attempt < retries:
                        await asyncio.sleep(0.2 * (attempt + 1))
                        continue
                    # Out of retries — raise the clean, classified error.
                    raise

            # Unreachable in practice, but keeps control flow explicit.
            raise InfrastructureError(
                f"Redis unavailable after {retries + 1} attempts"
            ) from last_error


redis_client = RedisClient()


async def get_redis() -> redis.Redis:
    """
    FastAPI dependency.

    Calls ensure_connected(), which now verifies liveness on every call
    (not just on first connect) and raises InfrastructureError — caught
    globally by main.py's exception handler — if Redis is genuinely down.
    """
    return await redis_client.ensure_connected()