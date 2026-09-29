"""
Redis-backed email OTP store. Scoped by (email, purpose) so the same
user can have separate OTPs for "password_reset" and "login_2fa".
"""
import secrets
from typing import Optional

from App.core.RedisConnector import redis_client
from App.core.settings import settings
from App.core.LoggingInit import get_core_logger

logger = get_core_logger(__name__)


def _key(email: str, purpose: str) -> str:
    return f"email_otp:{purpose}:{email.lower()}"


def _attempts_key(email: str, purpose: str) -> str:
    return f"email_otp_attempts:{purpose}:{email.lower()}"


async def create_otp(email: str, purpose: str) -> str:
    """Generate a 6-digit OTP, store, return it. Resets attempts counter."""
    otp = f"{secrets.randbelow(1_000_000):06d}"
    c = await redis_client.ensure_connected()
    await c.set(
        _key(email, purpose),
        otp,
        ex=settings.EMAIL_OTP_TTL_SECONDS,
    )
    await c.delete(_attempts_key(email, purpose))
    logger.info(f"Email OTP created for {email} purpose={purpose}")
    return otp


async def verify_otp(email: str, purpose: str, code: str) -> bool:
    """
    Verify and consume on success. Bumps attempt counter on failure.
    Returns True only for a correct code that hasn't hit the attempt cap.
    """
    c = await redis_client.ensure_connected()

    # Attempt limit
    attempts_key = _attempts_key(email, purpose)
    attempts = await c.incr(attempts_key)
    if attempts == 1:
        await c.expire(attempts_key, settings.EMAIL_OTP_TTL_SECONDS)

    if attempts > settings.EMAIL_OTP_MAX_ATTEMPTS:
        logger.warning(f"Email OTP max attempts exceeded: {email} purpose={purpose}")
        await c.delete(_key(email, purpose))
        return False

    stored = await c.get(_key(email, purpose))
    if not stored:
        return False

    if not secrets.compare_digest(stored, code):
        return False

    # Success — consume
    await c.delete(_key(email, purpose))
    await c.delete(attempts_key)
    logger.info(f"Email OTP verified: {email} purpose={purpose}")
    return True