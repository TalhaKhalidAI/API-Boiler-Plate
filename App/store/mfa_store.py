"""
MFA challenge + email OTP store (Redis-backed).

This module handles ONLY ephemeral state:
  - mfa_challenge:{id}  → {user_id, method, attempts}  TTL 5min
  - mfa_email_otp:{id}  → 6-digit code                  TTL 5min

Permanent state (TOTP secret) lives in the DB on users.mfa_secret_encrypted.
"""
import json
import secrets
from typing import Optional, Literal

from App.core.RedisConnector import redis_client
from App.core.settings import settings
from App.core.LoggingInit import get_core_logger

logger = get_core_logger(__name__)

MFAMethod = Literal["totp", "email"]


def _challenge_key(challenge_id: str) -> str:
    return f"mfa_challenge:{challenge_id}"


def _email_otp_key(challenge_id: str) -> str:
    return f"mfa_email_otp:{challenge_id}"


# ============================================================================
# Challenge lifecycle (shared by TOTP and email paths)
# ============================================================================

async def create_challenge(user_id: int, method: MFAMethod) -> str:
    """Issue a fresh challenge_id. Called after password verification."""
    challenge_id = secrets.token_urlsafe(32)
    c = await redis_client.ensure_connected()
    await c.set(
        _challenge_key(challenge_id),
        json.dumps({"user_id": user_id, "method": method, "attempts": 0}),
        ex=settings.MFA_CHALLENGE_TTL_SECONDS,
    )
    logger.info(f"MFA challenge created: user={user_id} method={method}")
    return challenge_id


async def get_challenge(challenge_id: str) -> Optional[dict]:
    c = await redis_client.ensure_connected()
    raw = await c.get(_challenge_key(challenge_id))
    return json.loads(raw) if raw else None


async def bump_attempts(challenge_id: str) -> int:
    """
    Increment failure count. If it hits the cap, delete the challenge
    immediately so the client MUST restart login.
    Returns the new count, or -1 if challenge no longer exists.
    """
    c = await redis_client.ensure_connected()
    raw = await c.get(_challenge_key(challenge_id))
    if not raw:
        return -1

    data = json.loads(raw)
    data["attempts"] += 1

    if data["attempts"] >= settings.MFA_MAX_ATTEMPTS:
        await c.delete(_challenge_key(challenge_id))
        logger.warning(f"MFA challenge killed after max attempts: {challenge_id[:8]}…")
        return data["attempts"]

    await c.set(
        _challenge_key(challenge_id),
        json.dumps(data),
        ex=settings.MFA_CHALLENGE_TTL_SECONDS,
    )
    return data["attempts"]


async def consume_challenge(challenge_id: str) -> None:
    """Delete on success. Also cleans up any email OTP attached to it."""
    c = await redis_client.ensure_connected()
    await c.delete(_challenge_key(challenge_id))
    await c.delete(_email_otp_key(challenge_id))


# ============================================================================
# Email OTP (only used when method == "email")
# ============================================================================

async def store_email_otp(challenge_id: str) -> str:
    """Generate a 6-digit OTP, store against the challenge. Return it for sending."""
    otp = f"{secrets.randbelow(1_000_000):06d}"
    c = await redis_client.ensure_connected()
    await c.set(
        _email_otp_key(challenge_id),
        otp,
        ex=settings.MFA_CHALLENGE_TTL_SECONDS,
    )
    return otp


async def verify_email_otp(challenge_id: str, user_code: str) -> bool:
    """Constant-time compare. Consumes on success."""
    c = await redis_client.ensure_connected()
    stored = await c.get(_email_otp_key(challenge_id))
    if not stored:
        return False
    if not secrets.compare_digest(stored, user_code):
        return False
    await c.delete(_email_otp_key(challenge_id))
    return True