# App/services/auth_service.py
from datetime import datetime, timezone
import uuid
from typing import Any, Dict, Optional

from redis.exceptions import RedisError
import jwt

from App.api.dependencies.auth import (
    authenticate_user,
    create_access_token,
    create_refresh_token,
    decode_jwt_ignore_expiry,
    get_password_hash,
    validate_password_strength,
)
from App.store import token_store
from App.core.exceptions import DomainError, DuplicateEmailError,RateLimitError,InfrastructureError,MFARequiredError,InvalidMFACodeError
from App.core.settings import settings
from App.repository.UserRepository import UserRepository
from sqlalchemy.ext.asyncio import AsyncSession
from App.core.LoggingInit import get_core_logger
from App.services.mfa_service import MFAService
logger=get_core_logger(__name__)
class AuthService:
    """Authentication business logic kept separate from HTTP routes."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def login_user(
        self,
        username: str,
        password: str,
        mfa_code: Optional[str] = None,
        ip: str = "unknown",
    ) -> Optional[Dict[str, Any]]:
        """
        Single-step login with optional MFA.

        Behavior:
          - No MFA enabled on user → issue tokens immediately.
          - MFA enabled, no code        → raise MFARequiredError(method=...)
          - MFA enabled, bad code       → raise InvalidMFACodeError
          - MFA enabled, valid code     → issue tokens

        MFA method is chosen per-user (users.mfa_method):
          - "totp"  → pyotp against stored secret
          - "email" → Redis OTP from /auth/email_otp/send with purpose='login_2fa'
        """
        # ── Rate limit ──────────────────────────────────────────────
        try:
            allowed = await token_store.check_login_rate(
                identifier=username,
                ip=ip,
                max_attempts=settings.MAX_LOGIN_ATTEMPTS or 4,
                window=settings.REFRESH_TOKEN_TTL_SECONDS or 300,
            )
        except (RedisError, RuntimeError) as exc:
            raise InfrastructureError("Redis unavailable during login") from exc

        if not allowed:
            raise RateLimitError("Too many login attempts. Try again in a few minutes.")

        # ── Password verification ───────────────────────────────────
        user = await authenticate_user(username, password, self.db)
        if not user:
            return None

        # ── MFA gate ────────────────────────────────────────────────
        if settings.ENABLE_MFA:
            repo = UserRepository(self.db)
            full_user = await repo.get_by_id(user["id"])

            if full_user and full_user.mfa_enabled:
                method = full_user.mfa_method or "totp"

                if not mfa_code:
                    raise MFARequiredError(method=method)

                # MFAService handles both "totp" and "email" internally.
                
                ok = await MFAService(self.db).verify_for_login(full_user, mfa_code)
                if not ok:
                    logger.warning(
                        f"MFA verification failed for user {user['id']} method={method}"
                    )
                    raise InvalidMFACodeError()

                logger.info(
                    f"MFA verified for user {user['id']} method={method}"
                )

        # ── Issue tokens (no MFA or MFA passed) ─────────────────────
        return await self.issue_tokens_for_user(
            user_id=user["id"],
            email=user["email"],
            name=user["name"],
            role=user["role"],
        )

    async def issue_tokens_for_user(
        self,
        user_id: int,
        email: str,
        name: str,
        role: str,
    ) -> Dict[str, Any]:
        """Unchanged — token issuance logic."""
        family_id = str(uuid.uuid4())

        access_token = create_access_token(
            data={
                "type": "token",
                "sub": email,
                "user_id": user_id,
                "name": name,
                "role": role,
            }
        )
        refresh_token = create_refresh_token(
            data={
                "type": "rf_token",
                "sub": email,
                "user_id": user_id,
            },
            family_id=family_id,
        )

        jti = jwt.decode(
            refresh_token,
            settings.secret_key_str,
            algorithms=[settings.ALGORITHM],
        )["jti"]

        try:
            await token_store.store_refresh(
                jti=jti,
                user_id=user_id,
                family_id=family_id,
                ttl=settings.REFRESH_TOKEN_TTL_SECONDS,
            )
            await token_store.track_family_for_user(
                user_id=user_id,
                family_id=family_id,
                ttl=settings.REFRESH_TOKEN_TTL_SECONDS,
            )
        except (RedisError, RuntimeError) as exc:
            raise InfrastructureError("Redis unavailable during login") from exc

        return {
            "user": {
                "id": user_id,
                "email": email,
                "name": name,
                "role": role,
            },
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }

 
    async def register_user(self, user_data) -> Any:
        """Validate and create a new user without exposing repo logic to the route."""
        repo = UserRepository(self.db)

        if await repo.exists_by_email(user_data.email):
            raise DuplicateEmailError("Email already registered")

        user_dict = user_data.model_dump()
        if "password" not in user_dict or not user_dict["password"]:
            raise DomainError("Password is required")
        if not validate_password_strength(user_dict["password"]):
            raise DomainError(
                "Password must be at least 8 characters with uppercase, lowercase, digit, and special character"
            )

        user_dict["password_hash"] = get_password_hash(user_dict["password"])
        del user_dict["password"]

        user_dict["user_role"] = "user"
        user_dict["is_active"] = True
        user_dict["permissions"] = {
            "user.view.self": True,
            "user.update.self": True,
            "user.update.email": True,
            "user.update.password": True,
            "user.update.profile": True,
            "user.delete.self": True,
            "user.history.view": True,
            "user.history.delete": True,
            "user.self.enable": True,
            "user.disable.self": True,
        }

        return await repo.create(user_dict)

    async def revoke_session(self, refresh_token_value: Optional[str]) -> bool:
        """
        Revoke the active session.
        Also revokes the entire rotation FAMILY so rotated descendants are dead too.
        """
        if not refresh_token_value:
            return False

        payload = decode_jwt_ignore_expiry(refresh_token_value)
        jti = payload.get("jti") if payload else None
        exp = payload.get("exp") if payload else None
        family = payload.get("family") if payload else None
        user_id = payload.get("user_id") if payload else None

        if not jti:
            return False

        ttl = (
            max(int(exp - datetime.now(timezone.utc).timestamp()), 1)
            if exp
            else settings.REFRESH_TOKEN_TTL_SECONDS
        )
        try:
            await token_store.revoke_refresh(jti, ttl)
            if family:
                await token_store.revoke_family(
                    family,
                    user_id=user_id,
                    revoke_ttl=settings.REFRESH_TOKEN_TTL_SECONDS,
                )
                # Remove from user's tracking set — this family is now dead
                if user_id:
                    await token_store.forget_family_for_user(user_id, family)
            return True
        except (RedisError, RuntimeError) as exc:
            raise InfrastructureError("Redis unavailable during logout") from exc
    # ========================================================================
    # NEW: revoke ALL sessions for a user (logout-everywhere)
    # ========================================================================
    async def revoke_all_sessions_for_user(self, user_id: int) -> int:
        """
        Kill every refresh family currently tracked for this user.

        Used when logout is called without a specific refresh token — e.g. a
        Bearer client sends only its access token. Without this, the client's
        refresh token would survive logout and could mint new sessions.

        Returns the number of families revoked.
        """
        try:
            families = await token_store.get_user_families(user_id)
            for fam in families:
                await token_store.revoke_family(
                    fam,
                    user_id=user_id,
                    revoke_ttl=settings.REFRESH_TOKEN_TTL_SECONDS,
                )
            # Clear the tracking set itself
            await token_store.clear_user_families(user_id)

          
            return len(families)
        except (RedisError, RuntimeError) as exc:
            raise InfrastructureError("Redis unavailable during logout-everywhere") from exc