# App/middleware/rate_limit_middleware.py
"""
Global Redis-backed rate limiter middleware.

- Applies to ALL routes automatically (no decorators needed)
- Per-endpoint overrides via PER_ENDPOINT dict
- Whitelist via EXEMPT_PATHS
- Fails OPEN if Redis is down (doesn't block traffic)
- Adds X-RateLimit-* headers to every response
- Uses fixed time buckets in Redis

Client IP resolution:
    X-Forwarded-For is honored ONLY when the immediate TCP peer is listed in
    the TRUSTED_PROXIES environment variable (comma-separated IPs or CIDRs).
    If TRUSTED_PROXIES is empty, XFF is ignored entirely and request.client.host
    is used. This prevents a direct client from spoofing the header to obtain a
    fresh rate-limit bucket per request.

Usage in main.py:
    from App.middleware.rate_limit_middleware import GlobalRateLimitMiddleware
    app.add_middleware(GlobalRateLimitMiddleware, default_limit="100/minute")
"""

import ipaddress
import os
import time
from typing import Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from App.core.RedisConnector import redis_client
from App.core.LoggingInit import get_core_logger

logger = get_core_logger(__name__)


# ============================================================
# Trusted proxy resolution
# ============================================================

def _trusted_proxies() -> Tuple[str, ...]:
    """
    Read TRUSTED_PROXIES from the environment.

    Returns a tuple of IP strings and/or CIDR strings. Empty tuple means
    "ignore X-Forwarded-For entirely" — the safe default for deployments
    where clients connect directly to the app.
    """
    raw = os.getenv("TRUSTED_PROXIES", "").strip()
    if not raw:
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _is_trusted_peer(addr: str) -> bool:
    """True if addr matches any entry in TRUSTED_PROXIES (exact or CIDR)."""
    for entry in _trusted_proxies():
        if entry == addr:
            return True
        if "/" in entry:
            try:
                if ipaddress.ip_address(addr) in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                # Malformed CIDR in config — skip it, do not crash.
                continue
    return False


def resolve_client_ip(request: Request) -> str:
    """
    Trusted-proxy-aware client IP resolution.

    When the immediate peer is a trusted proxy, walk X-Forwarded-For
    right-to-left, skipping trusted hops. The first non-trusted hop
    is the real client.
    """
    peer = request.client.host if request.client else "unknown"

    if not _is_trusted_peer(peer):
        return peer

    xff = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        if not _is_trusted_peer(hop):
            return hop

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    return peer


# ============================================================
# Limit parsing
# ============================================================

_PERIODS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
}


def parse_limit(limit_str: str) -> Tuple[int, int]:
    """
    Parse "100/minute" → (100, 60).
    Raises ValueError on invalid format.
    """
    try:
        count_part, period_part = limit_str.strip().lower().split("/")
        count = int(count_part.strip())
        period = period_part.strip()

        if period not in _PERIODS:
            raise ValueError(f"Unknown period '{period}'. Use: {list(_PERIODS.keys())}")

        if count <= 0:
            raise ValueError(f"Count must be > 0, got {count}")

        return count, _PERIODS[period]
    except Exception as e:
        raise ValueError(f"Invalid rate limit '{limit_str}': {e}") from e


# ============================================================
# Global rate limit middleware
# ============================================================

class GlobalRateLimitMiddleware(BaseHTTPMiddleware):
    """
    Per-IP rate limiter, global by default, with per-endpoint overrides.

    The rate limit is keyed on: (client_ip, method, path, time_bucket).
    Uses INCR + EXPIRE for atomic counting.

    client_ip is derived via resolve_client_ip(), which honors XFF only when
    the immediate peer is a trusted proxy.
    """

    # Paths that are NEVER rate limited (exact prefix match below).
    EXEMPT_PATHS = (
        "/health",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
    )

    # Per-endpoint overrides.
    # Format: "METHOD /exact/path" → "limit"
    # Longest matching prefix wins.
    PER_ENDPOINT = {
        "POST /app/v1/auth/basic_auth/login":       "10/minute",
        "POST /app/v1/auth/basic_auth/signup":      "5/hour",
        "POST /app/v1/auth/basic_auth/logout":      "30/minute",
        "POST /app/v1/users/users_config/refresh":  "60/minute",
        "POST /app/v1/admin/admin_access/account/temp_token": "10/minute",
        "POST /app/v1/admin/admin_access/account/restore":    "10/minute",
        "DELETE /app/v1/admin/admin_access/account":          "10/minute",
        "PUT /app/v1/admin/admin_access/account/password":    "10/minute",
    }

    def __init__(self, app, default_limit: str = "100/minute"):
        super().__init__(app)

        self.default_count, self.default_window = parse_limit(default_limit)
        self.endpoint_limits = {
            key: parse_limit(limit) for key, limit in self.PER_ENDPOINT.items()
        }

        trusted = _trusted_proxies()
        logger.info(
            f"GlobalRateLimitMiddleware initialized: "
            f"default={default_limit}, "
            f"overrides={len(self.endpoint_limits)}, "
            f"trusted_proxies={list(trusted) or 'none (XFF ignored)'}"
        )

    # ---------- helpers ----------

    def _is_exempt(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in self.EXEMPT_PATHS)

    def _get_limit(self, method: str, path: str) -> Tuple[int, int]:
        """Longest-prefix match against PER_ENDPOINT. Falls back to default."""
        best: Optional[Tuple[int, int]] = None
        best_len = -1

        for key, (count, window) in self.endpoint_limits.items():
            k_method, _, k_path = key.partition(" ")
            if k_method != method:
                continue
            if not path.startswith(k_path):
                continue
            if len(k_path) > best_len:
                best = (count, window)
                best_len = len(k_path)

        return best if best else (self.default_count, self.default_window)

    def _client_ip(self, request: Request) -> str:
        """
        Client IP resolution. Delegates to the shared trusted-proxy-aware
        resolver — do not inline the XFF logic here.
        """
        return resolve_client_ip(request)

    # ---------- main dispatch ----------

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 1. Skip exempt paths
        if self._is_exempt(path):
            return await call_next(request)

        # 2. Determine limit
        method = request.method.upper()
        limit, window = self._get_limit(method, path)

        # 3. Build Redis key
        client_ip = self._client_ip(request)
        bucket = int(time.time() // window)
        key = f"rl:{client_ip}:{method}:{path}:{bucket}"

        # 4. Redis check — INCR + EXPIRE in one pipeline so the key cannot
        #    be left without a TTL if the process dies between calls.
        try:
            c = await redis_client.ensure_connected()
            pipe = c.pipeline()
            pipe.incr(key)
            pipe.expire(key, window + 1)
            results = await pipe.execute()
            count = int(results[0])

        except Exception as e:
            # Redis down → FAIL OPEN. Never block traffic on limiter failure.
            logger.error(f"Rate limiter Redis error (failing open): {e}")
            return await call_next(request)

        # 5. Compute response headers
        remaining = max(0, limit - count)
        reset_at = (bucket + 1) * window
        retry_after = max(1, reset_at - int(time.time()))

        # 6. Over limit → 429
        if count > limit:
            logger.warning(
                f"Rate limit exceeded: ip={client_ip} method={method} "
                f"path={path} count={count}/{limit}"
            )
            return JSONResponse(
                status_code=429,
                headers={
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset_at),
                    "Retry-After": str(retry_after),
                },
                content={
                    "error": "rate_limit_exceeded",
                    "message": f"Too many requests. Limit: {limit} per {window}s.",
                    "retry_after": retry_after,
                },
            )

        # 7. Under limit → pass through, add headers (setdefault so we never
        #    clobber a header set by a downstream handler).
        response = await call_next(request)
        response.headers.setdefault("X-RateLimit-Limit", str(limit))
        response.headers.setdefault("X-RateLimit-Remaining", str(remaining))
        response.headers.setdefault("X-RateLimit-Reset", str(reset_at))
        return response