
# App/api/dependencies/auth.py
"""
Authentication and authorization dependencies.

Exception policy (applies to every function in this file):
    - Raise HTTPException only for HTTP-level DECISIONS (401/403/404).
      These are deliberate responses to a known condition, not caught
      exceptions. Example: "no token supplied" → 401.
    - Let infrastructure errors (SQLAlchemyError, RedisError, library
      bugs, missing config) propagate to main.py's global handlers.
      Those handlers return a consistent 503 for DB/Redis downtime and
      a request_id-tagged 500 for genuine bugs.
    - Do NOT catch `Exception` anywhere in this file. Doing so hides
      the real cause and breaks the global handler contract.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
import uuid

import jwt
from argon2 import PasswordHasher, exceptions as argon2_exceptions
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyCookie
from sqlalchemy.ext.asyncio import AsyncSession

from App.core.settings import settings
from App.core.Connector import get_db
from App.repository.UserRepository import UserRepository
from App.core.LoggingInit import get_core_logger
from App.core import token_store
logger = get_core_logger(__name__)

# ============================================================================
# Password hasher
# ============================================================================
pwd_context = PasswordHasher(
    memory_cost=settings.MEMORY_COST,
    parallelism=settings.PARALLELISM,
    hash_len=settings.HASH_LENGTH,
    salt_len=settings.SALT_LENGTH,
)
# Timing-safe dummy hash for the "user not found" path in authenticate_user.
# Computed once at import so the code path is constant-time regardless of
# whether the user exists.
DUMMY_PASSWORD_HASH = pwd_context.hash("timing-safe-dummy-password")

# ============================================================================
# Security schemes
# ============================================================================
oauth2_scheme = HTTPBearer(auto_error=False)
cookie_scheme = APIKeyCookie(name="CSO", auto_error=False)
refresh_cookie_scheme = APIKeyCookie(name="refresh_token", auto_error=False)


# ============================================================================
# Password functions
# ============================================================================
def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a password against an Argon2 hash.

    Returns False on mismatch (the normal "wrong password" case).
    Any other failure (malformed hash, config error, library bug)
    propagates to main.py's handlers instead of being masked.
    """
    try:
        return pwd_context.verify(hash=hashed_password, password=plain_password)
    except argon2_exceptions.VerifyMismatchError:
        return False


def get_password_hash(password: str) -> str:
    """Hash a password with Argon2."""
    return pwd_context.hash(password=password)


# ============================================================================
# Token functions
# ============================================================================
def create_access_token(
    data: Dict[str, Any],
    expires_delta: Optional[timedelta] = None,
) -> str:
    """
    Create a JWT access token.

    Any failure (missing SECRET_KEY, malformed data, library error)
    propagates to main.py's handlers.
    """
    to_encode = data.copy()

    expire = datetime.now(timezone.utc) + (
        expires_delta
        if expires_delta
        else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    to_encode.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "access",
        "jti": str(uuid.uuid4()),
    })

    encoded_jwt = jwt.encode(
        to_encode,
        settings.secret_key_str,
        algorithm=settings.ALGORITHM,
    )

    logger.debug(f"Created access token for user: {data.get('sub', 'unknown')}")
    return encoded_jwt


def decode_jwt(token: str) -> Optional[Dict[str, Any]]:
    """
    Decode and validate a JWT.

    - Expired signature  → 401 "Token has expired"
    - Any other JWT issue → 401 "Invalid token"

    Any non-JWT failure (missing SECRET_KEY, library bug) propagates.
    """
    try:
        payload = jwt.decode(
            token,
            settings.secret_key_str,
            algorithms=[settings.ALGORITHM],
        )
        return payload
    except jwt.ExpiredSignatureError:
        logger.debug("Token expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        logger.debug(f"JWT decode failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def decode_jwt_ignore_expiry(token: str) -> Optional[Dict[str, Any]]:
    """
    Decode a JWT verifying the signature only — used when we need to
    inspect claims (e.g. user_id) from an expired token.

    Returns None if the token is malformed. Any other failure propagates.
    """
    try:
        return jwt.decode(
            token,
            settings.secret_key_str,
            algorithms=[settings.ALGORITHM],
            options={"verify_exp": False},
        )
    except jwt.InvalidTokenError as e:
        logger.warning(f"JWT decode (ignore-expiry) failed: {e}")
        return None


# ============================================================================
# Shared token extraction / validation helpers
# ============================================================================
def _extract_token(
    credentials: Optional[HTTPAuthorizationCredentials],
    cookie_auth: Optional[str],
) -> str:
    """
    Return the bearer token from either the Authorization header or the
    auth cookie. Raises 401 if neither is present.
    """
    token = credentials.credentials if credentials else cookie_auth

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authentication token found",
        )

    if token.startswith("Bearer "):
        token = token[7:]

    return token


async def _validate_token_payload(payload: Dict[str, Any]) -> None:
    """
    Shared post-decode validation for access tokens (both standard and SLT).

    - Rejects revoked tokens (Redis blocklist check).
    - Rejects non-access token types.
    - Rejects tokens missing `exp`.
    - Rejects expired tokens.
    - Rejects tokens missing `user_id`.

    Raises HTTPException(401) on any failure.
    NOTE: Redis errors propagate — main.py returns 503 for those.
    """
    jti = payload.get("jti")
    if jti and await token_store.is_access_blocked(jti):
        logger.warning(f"Blocklisted token used: jti={jti}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_type = payload.get("type")
    if token_type != "access":
        logger.warning(f"Non-access token used for authentication: type={token_type!r}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type. An access token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    exp = payload.get("exp")
    if exp is None:
        logger.warning("Token has no expiration time")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has no expiration",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if datetime.fromtimestamp(exp, timezone.utc) <= datetime.now(timezone.utc):
        logger.warning(f"Expired token used: expired at {exp}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not payload.get("user_id"):
        logger.warning("Token missing user_id")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )
def _user_dict(user, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build the auth context dictionary returned to route handlers."""
    return {
        "permissions": user.permissions,
        "id": user.id,
        "user_id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.user_role,
        "is_active": user.is_active,
        "disabled": user.disabled,
        "token_type": payload.get("type"),
        "token_purpose": payload.get("purpose"),
        "types": payload.get("types"),
    }


# ============================================================================
# Dependencies — SLT-aware (bypasses account-status checks)
# ============================================================================
async def get_current_user_slt(
    cookie_auth: Optional[str] = Depends(cookie_scheme),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """
    Auth dependency that ACCEPTS short-lived tokens (SLT).

    ⚠️  SLT tokens bypass all account-status checks (disabled / deleted).
        Their purpose is account recovery and password reset.
        They expire in 2 minutes and are meant to be single-use.

    DB errors (OperationalError, SQLAlchemyError) are NOT caught here —
    they propagate to main.py's handlers, which return a consistent 503
    for every endpoint that depends on this function.
    """
    token = _extract_token(credentials, cookie_auth)

    payload = decode_jwt(token)

    await _validate_token_payload(payload)

    is_slt_token = payload.get("types") == "slts"

    repo = UserRepository(db)
    user = await repo.get_by_id(payload["user_id"])

    if not user:
        logger.warning(f"User not found for ID: {payload['user_id']}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # SLT tokens skip the disabled/deleted checks on purpose.
    if not is_slt_token:
        if user.is_deleted:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

        if user.disabled or not user.is_active:
            logger.warning(f"Inactive user tried to authenticate: {user.id}")
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Account is disabled or inactive. Contact admin.",
            )

    logger.debug(f"Authenticated user (SLT-allowed): {user.email} (ID: {user.id})")
    return _user_dict(user, payload)


# ============================================================================
# Dependencies — standard access token only (SLT rejected)
# ============================================================================
async def get_current_user(
    cookie_auth: Optional[str] = Depends(cookie_scheme),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """
    Standard auth dependency.

    - Requires a regular access token.
    - Rejects short-lived tokens (SLT).
    - Enforces account-status checks (disabled, deleted).

    DB errors propagate to main.py's handlers.
    """
    token = _extract_token(credentials, cookie_auth)

    payload = decode_jwt(token)

    await _validate_token_payload(payload)

    if payload.get("types") == "slts":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Short term token not allowed",
        )

    repo = UserRepository(db)
    user = await repo.get_by_id(payload["user_id"])

    if not user:
        logger.warning(f"User not found for ID: {payload['user_id']}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if user.is_deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    if user.disabled or not user.is_active:
        logger.warning(f"Inactive user tried to authenticate: {user.id}")
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Account is disabled or inactive. Contact admin.",
        )

    logger.debug(f"Authenticated user: {user.email} (ID: {user.id})")
    return _user_dict(user, payload)


async def get_current_active_user(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """Reject if the authenticated user is disabled or inactive."""
    if current_user.get("disabled") or not current_user.get("is_active"):
        logger.warning(f"Inactive user access attempted: {current_user.get('email')}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user",
        )
    return current_user


async def get_admin_user(
    current_user: Dict[str, Any] = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Reject if the authenticated user is not an admin."""
    if current_user.get("role") != "admin":
        logger.warning(f"Non-admin user tried admin action: {current_user.get('email')}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions",
        )
    return current_user


async def get_current_user_optional(
    token: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> Optional[Dict[str, Any]]:
    """
    Optional auth. Returns the user if a valid token is present, else None.

    Only 401/403/404 from get_current_user are absorbed into None
    (that's the point of "optional"). Infrastructure errors still
    propagate — a DB outage should not silently look like "anonymous".
    """
    if not token:
        return None

    try:
        return await get_current_user(credentials=token, db=db)
    except HTTPException:
        return None


# ============================================================================
# Helpers
# ============================================================================
async def authenticate_user(
    uname: str,
    password: str,
    db: AsyncSession,
) -> Optional[Dict[str, Any]]:
    """
    Authenticate by name OR email.

    Returns the user dict on success, None on any of:
      - user not found
      - wrong password
      - account disabled / inactive / deleted

    Infrastructure failures (SQLAlchemyError) propagate so main.py
    returns 503 instead of "invalid credentials".
    """
    repo = UserRepository(db)

    # Lookup. DB errors propagate — do not catch.
    if "@" in uname:
        user = await repo.get_by_email(uname)
    else:
        user = await repo.get_by_name(uname)

    # Timing-safe: always run a hash comparison, even if the user is missing.
    if not user:
        logger.debug(f"Authentication failed: user not found - {uname}")
        verify_password(password, DUMMY_PASSWORD_HASH)
        return None

    if not verify_password(password, user.password_hash):
        logger.debug(f"Authentication failed: wrong password - {uname}")
        return None

    if user.disabled or not user.is_active:
        logger.debug(f"Authentication failed: account disabled - {uname}")
        return None

    if user.is_deleted:
        logger.debug(f"Authentication failed: account deleted - {uname}")
        return None

    logger.info(f"User authenticated successfully: {uname}")
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.user_role,
    }


def validate_password_strength(password: str) -> bool:
    """Check password strength (length + character classes)."""
    if len(password) < 8:
        return False
    if not any(c.isupper() for c in password):
        return False
    if not any(c.islower() for c in password):
        return False
    if not any(c.isdigit() for c in password):
        return False
    special_chars = "!@#$%^&*()_+-=[]{}|;:,.<>?`~"
    if not any(c in special_chars for c in password):
        return False
    return True


# ============================================================================
# Refresh token flow
# ============================================================================
def create_refresh_token(data: Dict[str, Any], family_id: str) -> str:
    """Create a refresh token with a 7-day expiry."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=7)

    to_encode.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "refresh",
        "jti": str(uuid.uuid4()),
        "family": family_id,
    })

    return jwt.encode(
        to_encode,
        settings.secret_key_str,
        algorithm=settings.ALGORITHM,
    )


async def issue_refresh_token(
    user_id: int,
    email: str,
    family_id: str,
) -> str:
    """
    Mint a refresh token and record it in the token store.

    Any Redis error (store_refresh, track_family_for_user) propagates
    to main.py's handlers.
    """
    refresh_token = create_refresh_token(
        data={"sub": email, "user_id": user_id},
        family_id=family_id,
    )

    payload = jwt.decode(
        refresh_token,
        settings.secret_key_str,
        algorithms=[settings.ALGORITHM],
    )
    jti = payload["jti"]

    await token_store.store_refresh(
        jti=jti,
        user_id=user_id,
        family_id=family_id,
        ttl=7 * 24 * 3600,
    )
    await token_store.track_family_for_user(
        user_id=user_id,
        family_id=family_id,
        ttl=7 * 24 * 3600,
    )

    return refresh_token


async def refresh_access_token(
    refresh_token: str,
    db: AsyncSession,
) -> Optional[Dict[str, str]]:
    """
    Validate and rotate a refresh token.

    Returns {access_token, refresh_token} on success.
    Returns None for any of the following expected outcomes:
      - token is not a valid JWT
      - token is not of type "refresh"
      - reuse detected (whole family revoked as a side effect)
      - token missing from the store (already consumed)
      - user is missing / disabled / inactive / deleted

    Infrastructure errors (RedisError, SQLAlchemyError) propagate —
    a Redis or DB outage must not be silently reported as "logged out".
    """
    try:
        payload = decode_jwt(refresh_token)
    except HTTPException:
        # decode_jwt raises 401 on expired/invalid — treat both as "no session".
        logger.debug("Refresh token invalid or expired")
        return None

    if payload.get("type") != "refresh":
        return None

    jti = payload.get("jti")
    family_id = payload.get("family")
    if not jti:
        return None

    # Reuse detection — if already revoked, kill the whole family.
    if await token_store.is_refresh_revoked(jti):
        logger.warning(f"Refresh reuse detected: jti={jti} family={family_id}")
        if family_id:
            await token_store.revoke_family(family_id)
        return None

    # Atomic consume — only one caller wins.
    meta = await token_store.consume_refresh(jti)
    if not meta:
        return None

    user_id = meta.get("user_id")
    if not user_id:
        return None

    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)
    if not user or user.disabled or not user.is_active or user.is_deleted:
        return None

    family = family_id or meta.get("family_id") or str(uuid.uuid4())

    new_access = create_access_token({
        "sub": user.email,
        "user_id": user.id,
        "name": user.name,
        "role": user.user_role,
    })

    new_refresh = create_refresh_token(
        data={"sub": user.email, "user_id": user.id},
        family_id=family,
    )

    new_jti = jwt.decode(
        new_refresh,
        settings.secret_key_str,
        algorithms=[settings.ALGORITHM],
    )["jti"]

    await token_store.store_refresh(
        jti=new_jti,
        user_id=user.id,
        family_id=family,
        ttl=7 * 24 * 3600,
    )

    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
    }
# ============================================================================
# Short-lived token (SLT)
# ============================================================================
async def create_short_live_token(
    user_id: int,
    db: AsyncSession,
    purpose: str = "restore_account",
) -> Optional[str]:
    """
    Create a short-lived (2 minute) token for a self-service action.

    Returns:
        str  — valid SLT
        None — the user does not exist

    Any other failure (DB down, missing SECRET_KEY, library error)
    propagates to main.py's handlers so the caller sees a real 503/500.
    """
    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)

    if not user:
        return None

    return create_access_token(
        data={
            "sub": user.email,
            "user_id": user.id,
            "name": user.name,
            "role": user.user_role,
            "types": "slts",
            "purpose": purpose,
            "jti": str(uuid.uuid4()),
        },
        expires_delta=timedelta(minutes=2),
    )