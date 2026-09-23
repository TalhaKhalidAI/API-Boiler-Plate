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
from App.core import token_store
from App.core.exceptions import DomainError, DuplicateEmailError,RateLimitError,InfrastructureError
from App.core.settings import settings
from App.repository.UserRepository import UserRepository
from sqlalchemy.ext.asyncio import AsyncSession

class AuthService:
    """Authentication business logic kept separate from HTTP routes."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def login_user(self, username: str, password: str,ip: str = "unknown",) -> Optional[Dict[str, Any]]:
        """Authenticate and issue both tokens for a valid user."""

        try:
            allowed = await token_store.check_login_rate(
                identifier=username,
                ip=ip,
                max_attempts=settings.MAX_LOGIN_ATTEMPTS or 4,
                window=300,
            )
        except (RedisError, RuntimeError) as exc:
            raise InfrastructureError("Redis unavailable during login") from exc
        if not allowed:
            raise RateLimitError("Too many login attempts. Try again in a few minutes.")
        user = await authenticate_user(username, password, self.db)
        if not user:
            return None
        family_id = str(uuid.uuid4())
        access_token = create_access_token(
            data={
                "type": "token",
                "sub": user["email"],
                "user_id": user["id"],
                "name": user["name"],
                "role": user["role"],
            }
        )
        refresh_token = create_refresh_token(
            data={
                "type": "rf_token",
                "sub": user["email"],
                "user_id": user["id"],
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
                user_id=user["id"],
                family_id=family_id,
                ttl=settings.REFRESH_TOKEN_TTL_SECONDS,
            )
            # NEW: track family under user for logout-everywhere
            await token_store.track_family_for_user(
                user_id=user["id"],
                family_id=family_id,
                ttl=settings.REFRESH_TOKEN_TTL_SECONDS,
                    )
        except (RedisError, RuntimeError) as exc:
            raise InfrastructureError("Redis unavailable during login") from exc

        return {
            "user": user,
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
                # ===== USER SELF-MANAGEMENT =====
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

            # ===== CHANNEL (own channel only) =====
            "channel.create.self": True,
            "channel.update.self": True,
            "channel.delete.self": True,
            "channel.self.enable": True,
            "channel.self.disable": True,
            "channel.members.manage.self": True,

            # ===== VIDEO (own channel only) =====
            "video.create.self": True,
            "video.update.self": True,
            "video.delete.self": True,
            "video.publish.self": True,

            # ===== PLAYLIST (own playlists only) =====
            "playlist.create.self": True,
            "playlist.update.self": True,
            "playlist.delete.self": True,

            # ===== COMMENT =====
            "comment.create.self": True,
            "comment.update.self": True,
            "comment.delete.self": True,
            "comment.pin.self": True,

            # ===== SUBSCRIPTION =====
            "subscription.create.self": True,
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
                    revoke_ttl=settings.REFRESH_TOKEN_TTL_SECONDS,
                )
            # Clear the tracking set itself
            await token_store.clear_user_families(user_id)

          
            return len(families)
        except (RedisError, RuntimeError) as exc:
            raise InfrastructureError("Redis unavailable during logout-everywhere") from exc