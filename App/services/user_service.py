from typing import Any, Dict, Optional
from  sqlalchemy.ext.asyncio import AsyncSession
from App.api.dependencies.auth import (
    decode_jwt,
    decode_jwt_ignore_expiry,
    refresh_access_token,
)
 

from App.core.LoggingInit import get_core_logger
from fastapi import UploadFile
from App.core.exceptions import DomainError, UserNotFoundError,ValidationError,PermissionDeniedError
from App.repository.UserRepository import UserRepository
from App.core.settings import settings
logger = get_core_logger(__name__)

class UserService:
    """Application business logic for user self-service and admin read flows."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def refresh_session(
        self,
        refresh_token_value: str,
        access_token_value: Optional[str] = None,
    ) -> Dict[str, str]:
        """Validate and rotate the refresh token for the active user session."""
        payload = decode_jwt(refresh_token_value)
        if not payload:
            raise DomainError("Invalid refresh token")

        if payload.get("types") == "slts":
            raise DomainError("Short-lived tokens cannot be used for refresh")

        if payload.get("type") != "refresh":
            raise DomainError("Invalid refresh token")

        user_id = payload.get("user_id")
        if not user_id:
            raise DomainError("Invalid refresh token payload")

        if access_token_value:
            at_payload = decode_jwt_ignore_expiry(access_token_value)
            if at_payload:
                at_user_id = at_payload.get("user_id")
                if at_user_id is not None and at_user_id != user_id:
                    raise DomainError("Refresh token does not match current session")

        result = await refresh_access_token(refresh_token_value, self.db)
        if not result:
            raise DomainError("Invalid or expired refresh token")

        return result

    async def get_profile(self, user_id: int):
        """Return the user profile for an authenticated user."""
        repo = UserRepository(self.db)
        user = await repo.get_by_id(user_id)
        if not user:
            raise UserNotFoundError(f"User {user_id} not found")
        return user

    async def list_users(self, skip: int = 0, limit: int = 100, search: Optional[str] = None):
        """Return paginated user listings for admin use."""
        repo = UserRepository(self.db)
        users = await repo.search_users(
            skip=skip,
            limit=limit,
            active_only=False,
            search=search,
        )
        total = await repo.count_users(active_only=False)
        return {
            "users": users,
            "total": total,
            "skip": skip,
            "limit": limit,
        }
