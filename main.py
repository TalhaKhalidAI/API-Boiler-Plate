# main.py
"""
FastAPI application entrypoint.

Middleware order (last added = outermost):
    CORSMiddleware            ← runs first on request, last on response
    KillSwitchMiddleware
    GlobalRateLimitMiddleware
    BodySizeLimitMiddleware
    RequestIDMiddleware       ← runs first on response, last on request
"""

from contextlib import asynccontextmanager
import os
import time
from tenacity import RetryError

from sqlalchemy import text
from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.exc import SQLAlchemyError, OperationalError
from App.api.v1 import app_router
from App.core.settings import settings
from App.core.LoggingInit import get_core_logger
from App.core.CreateAdmin import create_admin
from App.core.RedisConnector import redis_client
from App.core.Connector import AsyncSession, database, get_db
from App.middleware.rate_limit_middleware import GlobalRateLimitMiddleware
from App.middleware.kill_switch_middleware import KillSwitchMiddleware
from App.middleware.body_size_middleware import BodySizeLimitMiddleware
from App.middleware.request_id_middleware import RequestIDMiddleware
from App.storage.minio_storage import  minio_storage
from App.core.exceptions import (
    DomainError, InfrastructureError,
    UserNotFoundError, DuplicateEmailError, DuplicateNameError,MinIOError,PermissionDeniedError
)
from redis.exceptions import RedisError

logger = get_core_logger(__name__)


# ============================================================================
# Lifespan
# ============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Admin seeding moved to an Alembic data migration.
    # See App/api/databases/migrations/versions/<...>_seed_admin.py.
    # If you haven't migrated yet, keep the call but wrap it in try/except
    # so a race between workers doesn't kill startup. See CreateAdmin.py.
    try:
        await create_admin()
    except Exception:
        # Non-fatal: another worker may have already created the admin,
        # or the migration already seeded it. Log and continue.
        logger.exception("Admin seed skipped (likely already done)")

    try:
        await redis_client.connect()
        logger.info("App started")
    except Exception:
        logger.exception("Redis unavailable during startup; continuing in degraded mode")
        logger.warning("App started in degraded mode without Redis")
    try:
        minio_storage.connect()
        health = await minio_storage.health_check()
        if health["connected"]:
            logger.info("MinIO connected")
        else:
            logger.warning(f"MinIO unreachable at startup: {health.get('error')}")
    except Exception:
        logger.exception("MinIO init failed; object storage may be unavailable")
    yield

    # Graceful shutdown: close DB pool then Redis
    minio_storage.disconnect()
    await database.disconnect()
    await redis_client.disconnect()
    logger.info("App stopped")


app = FastAPI(
    title="API Basic Boilerplate",
    version="0.0.1",
    lifespan=lifespan,
)


# ============================================================================
# Exception handlers
# ============================================================================
_SENSITIVE_KEYS = {"password", "secret", "token", "refresh_token", "access_token",
                   "authorization", "api_key", "apikey"}


def _redact_errors(errors: list) -> list:
    """Strip sensitive input values from Pydantic validation errors."""
    out = []
    for err in errors:
        err = dict(err)
        loc = err.get("loc", ())

        # Redact if the failing field name looks sensitive
        if any(str(part).lower() in _SENSITIVE_KEYS for part in loc):
            err["input"] = "***redacted***"

        # Redact inside nested dict inputs
        if isinstance(err.get("input"), dict):
            err["input"] = {
                k: ("***redacted***" if str(k).lower() in _SENSITIVE_KEYS else v)
                for k, v in err["input"].items()
            }
        out.append(err)
    return out


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    req_id = getattr(request.state, "request_id", "-")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "status": exc.status_code,
            "request_id": req_id,
        },
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    req_id = getattr(request.state, "request_id", "-")
    return JSONResponse(
        status_code=422,
        content={
            "error": "Validation failed",
            "details": _redact_errors(exc.errors()),
            "request_id": req_id,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, "request_id", "-")
    logger.exception(
        f"[{req_id}] Unhandled exception on {request.method} {request.url.path}"
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "request_id": req_id,
        },
    )
# ============================================================================
# Database down — handled globally so every endpoint returns the same shape
# ============================================================================
@app.exception_handler(OperationalError)
async def db_operational_handler(request: Request, exc: OperationalError):
    req_id = getattr(request.state, "request_id", "-")
    logger.exception(f"[{req_id}] DB connection lost")
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "30"},
        content={
            "error": "database_unavailable",
            "message": "Database is temporarily unavailable. Please try again.",
            "status": 503,
            "request_id": req_id,
        },
    )


@app.exception_handler(SQLAlchemyError)
async def db_error_handler(request: Request, exc: SQLAlchemyError):
    req_id = getattr(request.state, "request_id", "-")
    logger.exception(f"[{req_id}] DB error")
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "30"},
        content={
            "error": "database_error",
            "message": "Database error. Please try again.",
            "status": 503,
            "request_id": req_id,
        },
    )


@app.exception_handler(RedisError)
async def redis_error_handler(request: Request, exc: RedisError):
    req_id = getattr(request.state, "request_id", "-")
    logger.exception(f"[{req_id}] Redis error")
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "30"},
        content={
            "error": "cache_unavailable",
            "message": "Cache service temporarily unavailable.",
            "status": 503,
            "request_id": req_id,
        },
    )


@app.exception_handler(MinIOError)
async def minio_error_handler(request: Request, exc: MinIOError):
    req_id = getattr(request.state, "request_id", "-")
    logger.exception(f"[{req_id}] MinIO error")
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "30"},
        content={
            "error": "storage_unavailable",
            "message": "Storage service temporarily unavailable.",
            "status": 503,
            "request_id": req_id,
        },
    )


# ============================================================================
# Domain errors — 400 by default, 404 for *NotFound
# ============================================================================
@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError):
    req_id = getattr(request.state, "request_id", "-")

    # InfrastructureError is a subclass of DomainError — must be checked first
    if isinstance(exc, InfrastructureError):
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "30"},
            content={
                "error": "infrastructure_unavailable",
                "message": str(exc),
                "status": 503,
                "request_id": req_id,
            },
        )

    # *NotFound → 404, *AlreadyExists / Duplicate → 409, everything else 400
    name = type(exc).__name__
    if name.endswith("NotFoundError"):
        code = 404
    elif "Already" in name or "Duplicate" in name:
        code = 409
    else:
        code = 400

    return JSONResponse(
        status_code=code,
        content={
            "error": "domain_error",
            "message": str(exc),
            "status": code,
            "request_id": req_id,
        },
    )

@app.exception_handler(PermissionDeniedError)
async def permission_denied_handler(request: Request, exc: PermissionDeniedError):
    req_id = getattr(request.state, "request_id", "-")
    return JSONResponse(
        status_code=403,
        content={
            "error": "forbidden",
            "message": str(exc),
            "status": 403,
            "request_id": req_id,
        },
    )

@app.exception_handler(RetryError)
async def retry_error_handler(request: Request, exc: RetryError):
    req_id = getattr(request.state, "request_id", "-")
    # RetryError wraps the last exception
    last_exc = exc.last_attempt.exception() if exc.last_attempt else exc
    logger.exception(f"[{req_id}] Retry exhausted: {last_exc}")
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "30"},
        content={
            "error": "infrastructure_unavailable",
            "message": str(last_exc) or "Service temporarily unavailable",
            "status": 503,
            "request_id": req_id,
        },
    )

# ============================================================================
# Middleware — order matters (last added = outermost)
# ============================================================================
app.add_middleware(RequestIDMiddleware, header_name="X-Request-ID")   # innermost
app.add_middleware(BodySizeLimitMiddleware, max_size=settings.MAX_BODY_SIZE)
app.add_middleware(
    GlobalRateLimitMiddleware,
    default_limit=settings.RATE_LIMIT_DEFAULT or "100/minute",
)
app.add_middleware(KillSwitchMiddleware, recovery_seconds=60)

ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()
]
app.add_middleware(                                                      # outermost
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


# ============================================================================
# Health checks
# ============================================================================
@app.get("/health", tags=["System"])
async def health_check(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Composite readiness check.

    Returns:
        200 healthy  — all dependencies OK
        200 degraded — service serving but a soft dependency (e.g. auto-kill) is off
        503 unhealthy — a hard dependency (DB, Redis) is down
    """
    checks = {
        "db": "unknown",
        "redis": "unknown",
        "auto_kill": "unknown",
        "migration": "unknown",
    }
    overall = "healthy"

    # -- DB --
    try:
        await db.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception:
        checks["db"] = "error"
        overall = "unhealthy"
        logger.exception("Health: DB check failed")

    # -- Redis --
    try:
        r = await redis_client.health_check()
        checks["redis"] = r["status"]
        if not r["connected"]:
            overall = "unhealthy"
    except Exception:
        checks["redis"] = "error"
        overall = "unhealthy"
        logger.exception("Health: Redis check failed")

    # -- Auto-kill state (so ops doesn't have to guess why traffic is 503) --
    try:
        until_raw = await redis_client.client.get("kill_switch:auto_kill_until")
        if until_raw and time.time() < float(until_raw):
            remaining = int(float(until_raw) - time.time())
            checks["auto_kill"] = f"active ({remaining}s)"
            if overall == "healthy":
                overall = "degraded"
        else:
            checks["auto_kill"] = "ok"
    except Exception:
        checks["auto_kill"] = "unknown"

    # -- Alembic version (catches "deployed code, forgot to migrate") --
    try:
        result = await db.execute(text("SELECT version_num FROM alembic_version"))
        checks["migration"] = result.scalar() or "unknown"
    except Exception:
        checks["migration"] = "unavailable"

    status_code = 200 if overall in ("healthy", "degraded") else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "version": "0.0.1",
            "checks": checks,
        },
    )


@app.get("/health/live", tags=["System"])
async def liveness():
    """Liveness probe — is the process alive? Does not touch dependencies."""
    return {"status": "alive"}


@app.get("/health/ready", tags=["System"])
async def readiness(db: AsyncSession = Depends(get_db)):
    """Readiness probe — alias for /health, used by K8s readiness probes."""
    return await health_check(request=None, db=db)


# ============================================================================
# Routers LAST
# ============================================================================
app.include_router(app_router, prefix="/app/v1")