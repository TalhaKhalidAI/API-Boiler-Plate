# App/store/idempotency_store.py
"""
Redis-backed store for Idempotency Keys.

Keys are prefixed with `idempotency:`.
State transitions:
  1. SET NX "processing" (acquires lock, TTL 30s)
  2. SET "response_payload" (replaces lock, TTL 24h)
"""
import json
from typing import Optional
from App.core.RedisConnector import redis_client
from App.core.LoggingInit import get_core_logger

logger = get_core_logger(__name__)

def _key(idempotency_key: str) -> str:
    return f"idempotency:{idempotency_key}"

async def acquire_lock(idempotency_key: str, lock_ttl: int = 30) -> bool:
    """
    Attempt to acquire the lock. Returns True if acquired, False if already exists.
    """
    c = await redis_client.ensure_connected()
    # NX ensures we only set it if it doesn't exist
    return await c.set(_key(idempotency_key), "processing", ex=lock_ttl, nx=True)

async def release_lock(idempotency_key: str) -> None:
    """Release the lock if the request failed and we want the client to be able to retry."""
    c = await redis_client.ensure_connected()
    # Only delete if it's currently in the "processing" state (don't delete a cached response)
    current = await c.get(_key(idempotency_key))
    if current == "processing":
        await c.delete(_key(idempotency_key))

async def save_response(idempotency_key: str, status_code: int, body: str, ttl: int = 86400) -> None:
    """Save the final HTTP response. Default TTL is 24 hours."""
    c = await redis_client.ensure_connected()
    data = json.dumps({"status": status_code, "body": body})
    await c.set(_key(idempotency_key), data, ex=ttl)
    logger.info(f"Idempotency response cached: key={idempotency_key} status={status_code}")

async def get_status(idempotency_key: str) -> Optional[str]:
    """Returns 'processing' if locked, or the JSON string if completed, or None."""
    c = await redis_client.ensure_connected()
    return await c.get(_key(idempotency_key))