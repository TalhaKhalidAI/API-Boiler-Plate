# App/api/v1/Users.py
"""
User self-service and admin endpoints.

Routes:
    POST /users_config/refresh   — rotate refresh token, issue new access token
    GET  /users_config/me        — current user's profile
    GET  /users_config/users     — list users (admin only)
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query, Body, Response
from fastapi.security import HTTPAuthorizationCredentials
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError

from App.core.LoggingInit import get_core_logger
from App.core.settings import settings
from App.core.Connector import get_db
from App.core.exceptions import UserNotFoundError, DomainError
from App.schemas.AuthScheema import TokenResponse, UserResponse
from App.services.user_service import UserService
from App.api.dependencies.auth import (
    get_current_active_user,
    get_admin_user,
    refresh_cookie_scheme,
    cookie_scheme,
    oauth2_scheme,
)

user_router = APIRouter(prefix="/users_config", tags=["Users"])
logger = get_core_logger(__name__)


# ============================================================================
# POST /users_config/refresh
# ============================================================================
@user_router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Refresh access + refresh tokens",
    description="Rotate refresh token and issue a new access token.",
)
async def refresh_token(
    res: Response,
    refresh_token_body: Optional[str] = Body(None, embed=True, alias="refresh_token"),
    refresh_token_cookie: Optional[str] = Depends(refresh_cookie_scheme),
    access_token_cookie: Optional[str] = Depends(cookie_scheme),
    access_credentials: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
):
    """Rotate refresh token, issue new access token.

    Errors from refresh_access_token propagate:
      - 401 for invalid/expired refresh (DomainError)
      - 503 for DB/Redis down
    """
    token = refresh_token_body or refresh_token_cookie
    if not token:
        from fastapi import HTTPException, status as http_status
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="No refresh token provided",
        )

    access_token = (
        access_credentials.credentials if access_credentials else access_token_cookie
    )

    result = await UserService(db).refresh_session(
        refresh_token_value=token,
        access_token_value=access_token,
    )

    if refresh_token_cookie:
        res.set_cookie(
            key="CSO",
            value=result["access_token"],
            httponly=True,
            secure=settings.COOKIE_SECURE,
            samesite="lax",
            max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            path="/",
        )
        res.set_cookie(
            key="refresh_token",
            value=result["refresh_token"],
            httponly=True,
            secure=settings.COOKIE_SECURE,
            samesite="strict",
            max_age=settings.REFRESH_TOKEN_TTL_SECONDS,
            path="/app/v1/users/users_config/refresh",
        )

    return TokenResponse(
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )

# ============================================================================
# GET /users_config/me
# ============================================================================
@user_router.get(
    "/me",
    response_model=UserResponse,
    summary="Get current user profile",
    description="Get detailed information about the currently authenticated user",
)
async def get_my_profile(
    current_user: Dict[str, Any] = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    user = await UserService(db).get_profile(current_user["id"])
    return UserResponse.model_validate(user)



# ============================================================================
# GET /users_config/users  (admin only)
# ============================================================================
@user_router.get(
    "/users",
    summary="List users (Admin only)",
    description="Get list of all users. Admin access required.",
)
async def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    search: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    data = await UserService(db).list_users(skip=skip, limit=limit, search=search)
    return {
        "users": [UserResponse.model_validate(u) for u in data["users"]],
        "total": data["total"],
        "skip": data["skip"],
        "limit": data["limit"],
    }