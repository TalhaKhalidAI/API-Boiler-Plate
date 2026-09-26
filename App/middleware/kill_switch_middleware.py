# App/middleware/kill_switch_middleware.py
"""
Kill switch middleware with two modes:

1. Manual kill:  settings.KILL_SWITCH_ENABLED = true → all requests 503
2. Auto kill:    infrastructure failure → N-second cooldown (Redis-backed)

Auto-kill is intentionally restricted to INFRASTRUCTURE failures only
(database, Redis, object storage). Application bugs (ValueError, KeyError,
pydantic errors, etc.) do NOT trip the kill switch — they are handled by
main.py's global exception handler and returned as a normal 500 to the
one caller that hit the bug. This prevents a single malformed request
from taking the entire API offline.

Redis-backed state means multiple workers share the same auto-kill flag.
Whitelisted paths (/health, /docs, etc.) always pass through.

NOTE: because main.py registers global exception handlers for
SQLAlchemyError / RedisError / InfrastructureError / RuntimeError,
those exceptions are converted to a Response by Starlette's
ExceptionMiddleware BEFORE they ever reach this middleware's dispatch().
So `engage_auto_kill()` below is called DIRECTLY from those handlers in
main.py, not from this middleware's except block. The except block here
is kept only as a fallback for the rare case something slips through.

Redis keys:
    kill_switch:auto_kill_until  →  Unix timestamp (float), TTL = recovery_seconds
"""

import asyncio
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware

from App.core.settings import settings
from App.core.LoggingInit import get_core_logger
from App.core.RedisConnector import redis_client
from App.core.exceptions import InfrastructureError

logger = get_core_logger(__name__)

_INFRA_EXCEPTIONS = (SQLAlchemyError, RedisError, InfrastructureError)

AUTO_KILL_KEY = "kill_switch:auto_kill_until"


# ============================================================================
# Module-level function — importable from main.py's exception handlers.
# This is the ONLY place that actually sets the Redis key in practice.
# ============================================================================
async def engage_auto_kill(recovery_seconds: int = 60) -> None:
    """
    Set the auto-kill flag in Redis. Uses SET NX so the first caller
    to trip the switch owns the window; a second failure does not extend it.

    Call this from main.py's exception handlers for genuine infra failures
    (OperationalError, SQLAlchemyError, RedisError, InfrastructureError).
    """
    try:
        until = time.time() + recovery_seconds
        c = await redis_client.ensure_connected()
        await c.set(AUTO_KILL_KEY, str(until), ex=recovery_seconds, nx=True)
        logger.error(
            f"Auto-kill engaged until {time.ctime(until)} "
            f"({recovery_seconds}s cooldown)"
        )
    except Exception as redis_err:
        logger.error(f"Failed to set auto-kill in Redis: {redis_err}")


class KillSwitchMiddleware(BaseHTTPMiddleware):
    """
    Kill switch with manual and auto modes.

    Manual mode is a config flag — the app refuses all traffic until the
    operator flips it off.

    Auto mode engages only on infrastructure exceptions. It sets a Redis
    key with a TTL; every worker checks the key on every request. When the
    TTL expires, traffic resumes automatically.
    """

    WHITELIST = ("/health", "/docs", "/redoc", "/openapi.json")

    def __init__(self, app, recovery_seconds: int = 60):
        super().__init__(app)
        self.recovery_seconds = recovery_seconds
        logger.info(
            f"KillSwitchMiddleware initialized: "
            f"manual={settings.KILL_SWITCH_ENABLED}, "
            f"recovery_seconds={recovery_seconds}, "
            f"whitelist={list(self.WHITELIST)}"
        )

    def _is_whitelisted(self, path: str) -> bool:
        return any(
            path == w or path.startswith(w + "/")
            for w in self.WHITELIST
        )

    async def _auto_kill_remaining(self) -> int:
        try:
            c = await redis_client.ensure_connected()
            until_raw = await c.get(AUTO_KILL_KEY)
            if not until_raw:
                return 0
            until = float(until_raw)
            remaining = int(until - time.time())
            return remaining if remaining > 0 else 0
        except Exception as e:
            logger.error(f"Auto-kill check failed (failing open): {e}")
            return 0

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if not self._is_whitelisted(path):
            if settings.KILL_SWITCH_ENABLED:
                logger.warning(f"Manual kill switch ON — blocking {path}")
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "Service unavailable (maintenance)",
                        "status": 503,
                        "type": "manual_kill",
                    },
                )

            remaining = await self._auto_kill_remaining()
            if remaining > 0:
                logger.warning(
                    f"Auto kill active — blocking {path} (retry in {remaining}s)"
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "Service recovering",
                        "status": 503,
                        "type": "auto_kill",
                        "retry_in": remaining,
                    },
                    headers={"Retry-After": str(remaining)},
                )

        try:
            return await call_next(request)

        except HTTPException:
            raise

        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            raise

        except _INFRA_EXCEPTIONS as e:
            # Fallback only — in the normal case main.py's handlers already
            # caught this and called engage_auto_kill() themselves before
            # this exception ever got here.
            req_id = getattr(request.state, "request_id", "-")
            logger.exception(
                f"Infrastructure failure — entering safety mode for "
                f"{self.recovery_seconds}s: {e}"
            )
            await engage_auto_kill(self.recovery_seconds)
            return JSONResponse(
                status_code=503,
                content={
                    "error": "Service temporarily unavailable",
                    "status": 503,
                    "request_id": req_id,
                },
                headers={"Retry-After": str(self.recovery_seconds)},
            )