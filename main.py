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
from fastapi.middleware.cors import  CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.exc import SQLAlchemyError, OperationalError
from sqlalchemy.exc import IntegrityError as SAIntegrityError
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
from App.core.exceptions import (
    IntegrityError,
    DomainError,
    InfrastructureError,
    PermissionDeniedError
)
from redis.exceptions import RedisError
from App.middleware.kill_switch_middleware import  engage_auto_kill
logger = get_core_logger(__name__)


# ============================================================================
# Lifespan
# ============================================================================
# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     # Admin seeding moved to an Alembic data migration.
#     # See App/api/databases/migrations/versions/<...>_seed_admin.py.
#     # If you haven't migrated yet, keep the call but wrap it in try/except
#     # so a race between workers doesn't kill startup. See CreateAdmin.py.
#     try:
#         await create_admin()
#     except Exception:
#         # Non-fatal: another worker may have already created the admin,
#         # or the migration already seeded it. Log and continue.
#         logger.exception("Admin seed skipped (likely already done)")

#     try:
#         await redis_client.connect()
#         logger.info("App started")
#     except Exception:
#         logger.exception("Redis unavailable during startup; continuing in degraded mode")
#         logger.warning("App started in degraded mode without Redis")
#     try:
#         minio_storage.connect()
#         health = await minio_storage.health_check()
#         if health["connected"]:
#             logger.info("MinIO connected")
#         else:
#             logger.warning(f"MinIO unreachable at startup: {health.get('error')}")
#     except Exception:
#         logger.exception("MinIO init failed; object storage may be unavailable")
#     yield

#     # Graceful shutdown: close DB pool then Redis
#     minio_storage.disconnect()
#     await database.disconnect()
#     await redis_client.disconnect()
#     logger.info("App stopped")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---------------------------------------------------------------
    # Admin seeding
    # ---------------------------------------------------------------
    try:
        await create_admin()
    except Exception:
        logger.exception("Admin seed skipped (likely already done)")

    # ---------------------------------------------------------------
    # Redis
    # ---------------------------------------------------------------
    try:
        await redis_client.connect()
        logger.info("Redis connected")
    except Exception:
        logger.exception("Redis unavailable during startup; continuing in degraded mode")

 
    logger.info("App started")
    yield

    try:
        await database.disconnect()
    except Exception:
        logger.exception("DB disconnect failed")

    try:
        await redis_client.disconnect()
    except Exception:
        logger.exception("Redis disconnect failed")

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


def _redact(value, depth: int = 0):
    if depth > 10:  # guard against pathological nesting
        return "***redacted***"
    if isinstance(value, dict):
        return {
            k: ("***redacted***" if str(k).lower() in _SENSITIVE_KEYS else _redact(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v, depth + 1) for v in value]
    return value


def _redact(value, depth: int = 0):
    if depth > 10:
        return "***redacted***"
    if isinstance(value, dict):
        return {
            k: ("***redacted***" if str(k).lower() in _SENSITIVE_KEYS else _redact(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v, depth + 1) for v in value]
    return value


def _redact_errors(errors: list) -> list:
    out = []
    for err in errors:
        err = dict(err)
        loc = err.get("loc", ())
        if any(str(part).lower() in _SENSITIVE_KEYS for part in loc):
            err["input"] = "***redacted***"
        else:
            err["input"] = _redact(err.get("input"))

        # FIX: Pydantic v2 puts a live exception in ctx.error — not JSON serializable
        ctx = err.get("ctx")
        if isinstance(ctx, dict):
            err["ctx"] = {
                k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
                for k, v in ctx.items()
            }
        elif ctx is not None and not isinstance(ctx, (str, int, float, bool, list, dict)):
            err["ctx"] = str(ctx)

        out.append(err)
    return out


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    req_id = getattr(request.state, "request_id", "-")
    try:
        details = _redact_errors(exc.errors())
    except Exception:
        logger.exception(f"[{req_id}] Failed to redact validation errors")
        details = [{"type": "validation_error", "msg": "Invalid request payload"}]

    try:
        return JSONResponse(
            status_code=422,
            content={"error": "Validation failed", "details": details, "request_id": req_id},
        )
    except Exception:
        logger.exception(f"[{req_id}] Failed to serialize validation response")
        return JSONResponse(
            status_code=422,
            content={"error": "Validation failed", "request_id": req_id},
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
    await engage_auto_kill() 
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
    await engage_auto_kill() 
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
    await engage_auto_kill()
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
@app.exception_handler(InfrastructureError)
async def infrastructure_error_handler(request: Request, exc: InfrastructureError):
    req_id = getattr(request.state, "request_id", "-")
    logger.warning(f"[{req_id}] Infrastructure error: {exc}")
    await engage_auto_kill()
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "30"},
        content={
            "error": "infrastructure_unavailable",
            "message": str(exc) or "Service temporarily unavailable",
            "status": 503,
            "request_id": req_id,
        },
    )

@app.exception_handler(RuntimeError)
async def runtime_error_handler(request: Request, exc: RuntimeError):
    """
    Safety net for RuntimeError. In our codebase, RuntimeError is only
    raised by RedisConnector when Redis is unavailable. Ideally that
    should be InfrastructureError, but until RedisConnector is fixed,
    this catches it and returns 503 instead of 500.
    """
    req_id = getattr(request.state, "request_id", "-")
    logger.exception(f"[{req_id}] RuntimeError: {exc}")
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "30"},
        content={
            "error": "infrastructure_unavailable",
            "message": str(exc) or "Service temporarily unavailable",
            "status": 503,
            "request_id": req_id,
        },
    )
@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    req_id = getattr(request.state, "request_id", "-")
    constraint = getattr(getattr(exc, "orig", None), "diag", None)
    constraint_name = getattr(constraint, "constraint_name", None) if constraint else None
    logger.warning(f"[{req_id}] IntegrityError constraint={constraint_name}")
    return JSONResponse(
        status_code=409,
        content={
            "error": "conflict",
            "message": "Resource already exists or violates a constraint",
            "constraint": constraint_name,
            "status": 409,
            "request_id": req_id,
        },
    )

@app.exception_handler(SAIntegrityError)
async def integrity_error_handler(request: Request, exc: SAIntegrityError):
    req_id = getattr(request.state, "request_id", "-")
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    constraint_name = getattr(diag, "constraint_name", None) if diag else None
    logger.warning(f"[{req_id}] IntegrityError constraint={constraint_name}")
    return JSONResponse(
        status_code=409,
        content={
            "error": "conflict",
            "message": "Resource already exists or violates a constraint",
            "constraint": constraint_name,
            "status": 409,
            "request_id": req_id,
        },
    )

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