# App/core/token_store.py
import json
from typing import Optional

from App.core.RedisConnector import redis_client
from App.core.redis_keys import (
    refresh_key,
    revoked_rt_key,
    family_key,
    blocklist_at_key,
    login_attempts_key,
       user_families_key, 
)


# ========== REFRESH TOKENS ==========
##Record that a refresh token exists. Called after login (and after rotation) so the token can later be revoked, tracked, or checked for reuse
async def store_refresh(jti: str, user_id: int, family_id: str, ttl: int) -> None:
    """Store refresh token metadata + track in family set + track family under user."""
    c = await redis_client.ensure_connected()
    pipe = c.pipeline()
    pipe.set(
        refresh_key(jti),
        json.dumps({"user_id": user_id, "family_id": family_id}),
        ex=ttl,
    )
    pipe.sadd(family_key(family_id), jti)
    pipe.expire(family_key(family_id), ttl)
    # Track which families belong to this user, so logout-everywhere can find them
    pipe.sadd(user_families_key(user_id), family_id)
    pipe.expire(user_families_key(user_id), ttl)
    await pipe.execute()

## use token and delete it from redis. If already used, return None. Otherwise, return the metadata and mark as revoked so reuse can be detected. its like janu dkh k dleete kar du ga
async def consume_refresh(jti: str, revoke_ttl: int = 7 * 24 * 3600) -> Optional[dict]:
    """
    Atomic get+delete. Returns None if already used/expired.
    Leaves a revoked marker so a second call is flagged as reuse.
    """
    c = await redis_client.ensure_connected()
    data = await c.getdel(refresh_key(jti))
    if not data:
        return None
    parsed = json.loads(data)
    # Mark used so reuse detection works next time
    await c.set(revoked_rt_key(jti), "1", ex=revoke_ttl)
    return parsed

### revoke a refresh token by marking it as revoked until its natural expiry. This is useful for logging out users or invalidating tokens without waiting for them to expire naturally. Janu k pic dkhi hilaya naha k dosto ko send kar iya or new neudes mungwai lol
async def revoke_refresh(jti: str, ttl: int) -> None:
    """Mark a refresh token as revoked and invalidate its stored payload immediately."""
    c = await redis_client.ensure_connected()
    await c.delete(refresh_key(jti))
    await c.set(revoked_rt_key(jti), "1", ex=ttl)


async def is_refresh_revoked(jti: str) -> bool:
    c = await redis_client.ensure_connected()
    return await c.exists(revoked_rt_key(jti)) == 1

## janu n nudes leak wali bat pakar li to use goli karwana 
async def revoke_family(family_id: str, revoke_ttl: int = 7 * 24 * 3600) -> None:
    """
    Kill every refresh token in a family.

    Marks each member revoked_rt:{jti} so reuse-detection fires for
    descendants too, not just the one token the client sent to logout.
    """
    c = await redis_client.ensure_connected()
    jtis = await c.smembers(family_key(family_id))
    if jtis:
        pipe = c.pipeline()
        for j in jtis:
            pipe.delete(refresh_key(j))
            pipe.set(revoked_rt_key(j), "1", ex=revoke_ttl)
        pipe.delete(family_key(family_id))
        await pipe.execute()

async def track_family_for_user(user_id: int, family_id: str, ttl: int) -> None:
    """Record that this family belongs to this user (used for logout-everywhere)."""
    c = await redis_client.ensure_connected()
    pipe = c.pipeline()
    pipe.sadd(user_families_key(user_id), family_id)
    pipe.expire(user_families_key(user_id), ttl)
    await pipe.execute()


async def get_user_families(user_id: int) -> list[str]:
    """Return every refresh family currently associated with this user."""
    c = await redis_client.ensure_connected()
    return list(await c.smembers(user_families_key(user_id)))


async def forget_family_for_user(user_id: int, family_id: str) -> None:
    """Remove a single family from the user's tracking set."""
    c = await redis_client.ensure_connected()
    await c.srem(user_families_key(user_id), family_id)

# ========== ACCESS TOKEN BLOCKLIST ==========

async def blocklist_access(jti: str, ttl: int) -> None:
    """Blocklist an access token jti until its natural expiry."""
    c = await redis_client.ensure_connected()
    await c.set(blocklist_at_key(jti), "1", ex=ttl)


async def is_access_blocked(jti: str) -> bool:
    c = await redis_client.ensure_connected()
    return await c.exists(blocklist_at_key(jti)) == 1


# ========== LOGIN RATE LIMIT ==========

# async def check_login_rate(
#     email: str, ip: str, max_attempts: int = 10, window: int = 300
# ) -> bool:
#     """Returns True if allowed, False if rate-limited."""
#     key = login_attempts_key(email, ip)
#     c = await redis_client.ensure_connected()
#     count = await c.incr(key)
#     if count == 1:
#         await c.expire(key, window)
#     return count <= max_attempts

async def clear_user_families(user_id: int) -> None:
    """Delete the user's family-tracking set entirely."""
    c = await redis_client.ensure_connected()
    await c.delete(user_families_key(user_id))
async def check_login_rate(
    identifier: str,
    ip: str,
    max_attempts: int = 5,
    window: int = 300,
) -> bool:
    """
    Returns True if allowed, False if rate-limited.

    Keyed on (normalized identifier, ip). Callers that want a second
    bucket keyed only on the identifier (to catch distributed sprays)
    should add it here.
    """
    ident = (identifier or "").strip().lower()
    key = login_attempts_key(ident, ip)
    c = await redis_client.ensure_connected()
    count = await c.incr(key)
    if count == 1:
        await c.expire(key, window)
    return count <= max_attempts

