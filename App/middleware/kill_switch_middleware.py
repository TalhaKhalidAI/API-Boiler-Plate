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

logger = get_core_logger(__name__)

# Import MinIO errors if available. Guarded so the middleware does not
# hard-depend on the storage layer — apps without MinIO still boot.
try:
    from App.core.exceptions import MinIOError
    _INFRA_EXCEPTIONS = (SQLAlchemyError, RedisError, MinIOError)
except ImportError:
    _INFRA_EXCEPTIONS = (SQLAlchemyError, RedisError)


class KillSwitchMiddleware(BaseHTTPMiddleware):
    """
    Kill switch with manual and auto modes.

    Manual mode is a config flag — the app refuses all traffic until the
    operator flips it off.

    Auto mode engages only on infrastructure exceptions. It sets a Redis
    key with a TTL; every worker checks the key on every request. When the
    TTL expires, traffic resumes automatically.
    """

    # Paths that bypass both kill modes. Matched as exact paths or as
    # path prefixes followed by "/".
    WHITELIST = ("/health", "/docs", "/redoc", "/openapi.json")

    AUTO_KILL_KEY = "kill_switch:auto_kill_until"

    def __init__(self, app, recovery_seconds: int = 60):
        super().__init__(app)
        self.recovery_seconds = recovery_seconds
        logger.info(
            f"KillSwitchMiddleware initialized: "
            f"manual={settings.KILL_SWITCH_ENABLED}, "
            f"recovery_seconds={recovery_seconds}, "
            f"whitelist={list(self.WHITELIST)}"
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _is_whitelisted(self, path: str) -> bool:
        """
        Exact match, or prefix match with a slash boundary.

        /health          → whitelisted
        /health/live     → whitelisted
        /healthsomething → NOT whitelisted
        """
        return any(
            path == w or path.startswith(w + "/")
            for w in self.WHITELIST
        )

    async def _auto_kill_remaining(self) -> int:
        """
        Return remaining seconds of the auto-kill window, or 0 if inactive.
        Never raises — Redis failures are treated as "no active kill".
        """
        try:
            c = await redis_client.ensure_connected()
            until_raw = await c.get(self.AUTO_KILL_KEY)
            if not until_raw:
                return 0
            until = float(until_raw)
            remaining = int(until - time.time())
            return remaining if remaining > 0 else 0
        except Exception as e:
            # Redis down → fail open. Do not block traffic on a broken limiter.
            logger.error(f"Auto-kill check failed (failing open): {e}")
            return 0

    async def _engage_auto_kill(self) -> None:
        """
        Set the auto-kill flag in Redis. Uses SET NX so the first worker
        to trip the switch owns the window; a second worker's failure
        does not extend it.
        """
        try:
            until = time.time() + self.recovery_seconds
            c = await redis_client.ensure_connected()
            await c.set(
                self.AUTO_KILL_KEY,
                str(until),
                ex=self.recovery_seconds,
                nx=True,
            )
            logger.error(
                f"Auto-kill engaged until {time.ctime(until)} "
                f"({self.recovery_seconds}s cooldown)"
            )
        except Exception as redis_err:
            logger.error(f"Failed to set auto-kill in Redis: {redis_err}")

    # ------------------------------------------------------------------
    # main dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if not self._is_whitelisted(path):
            # 1. Manual kill
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

            # 2. Auto kill (Redis-backed, multi-worker safe)
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

        # 3. Pass through
        try:
            return await call_next(request)

        except HTTPException:
            # Deliberate FastAPI response — never trip the kill switch.
            raise

        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            # Client hung up. Not a server fault. Do not trip the kill switch.
            raise

        except _INFRA_EXCEPTIONS as e:
            # Genuine infrastructure failure: DB, Redis, object storage.
            req_id = getattr(request.state, "request_id", "-")
            logger.exception(
                f"Infrastructure failure — entering safety mode for "
                f"{self.recovery_seconds}s: {e}"
            )
            await self._engage_auto_kill()
            return JSONResponse(
                status_code=503,
                content={
                    "error": "Service temporarily unavailable",
                    "status": 503,
                    "request_id": req_id,
                },
                headers={"Retry-After": str(self.recovery_seconds)},
            )

        # Everything else — application bugs, validation leaks, unexpected
        # exceptions. Let them propagate to main.py's global handler so the
        # caller gets a proper request-ID-tagged 500 and no other user is
        # affected.