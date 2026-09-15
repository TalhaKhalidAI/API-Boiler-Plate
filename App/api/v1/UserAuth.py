# App/api/v1/UserAuth.py
"""
Authentication endpoints.

Routes:
    POST /basic_auth/login    — authenticate, issue access + refresh tokens
    POST /basic_auth/signup   — register a new user
    POST /basic_auth/logout   — revoke refresh token, clear cookies
"""

from datetime import datetime, timezone
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query, Body, Response, Request
import jwt
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError

from App.schemas.AuthScheema import TokenResponse, UserResponse
from App.models.UserAuthModel import User, UpdateUser, LoginUser
from App.core.LoggingInit import get_core_logger
from App.core.Connector import get_db
from App.core import token_store
from App.core.settings import settings
from App.core.exceptions import (
    DuplicateEmailError,
    UserNotFoundError,
    DomainError,
)
from App.services.auth_service import AuthService
from App.api.dependencies.auth import (
    cookie_scheme,
    oauth2_scheme,
    refresh_cookie_scheme,
    decode_jwt_ignore_expiry
)
from fastapi.security import HTTPAuthorizationCredentials
logger = get_core_logger(__name__)

router = APIRouter(prefix="/basic_auth", tags=["Authentication"])


# ============================================================================
# POST /basic_auth/login
# ============================================================================
@router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    summary="User login",
    description="Authenticate user with name/email and password.",
)
async def login(
    request: Request,
    res: Response,
    form_data: LoginUser,
    db: AsyncSession = Depends(get_db),
    cookie_login: Optional[bool] = False,
):
    """Login endpoint supporting both JSON and cookie modes."""
    req_id = getattr(request.state, "request_id", "-")
    try:
        password = form_data.password.get_secret_value()
        result = await AuthService(db).login_user(form_data.username, password)

        if not result:
            logger.warning(f"[{req_id}] Failed login for {form_data.username}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password, or account is disabled",
                headers={"WWW-Authenticate": "Bearer"},
            )

        user = result["user"]
        access_token = result["access_token"]
        refresh_token = result["refresh_token"]
        expires_in_seconds = result["expires_in"]
        logger.info(f"[{req_id}] User logged in: {user['email']}")

        if cookie_login:
            res.set_cookie(
                key="CSO",
                value=access_token,
                httponly=True,
                secure=settings.COOKIE_SECURE,
                samesite="lax",
                max_age=expires_in_seconds,
                path="/",
            )
            res.set_cookie(
                key="refresh_token",
                value=refresh_token,
                httponly=True,
                secure=settings.COOKIE_SECURE,
                samesite="strict",
                max_age=settings.REFRESH_TOKEN_TTL_SECONDS,
                path="/app/v1/users/users_config/refresh",
            )
            return {
                "token": access_token,
                "refresh_token": refresh_token,
                "status": "success",
                "message": "Logged in successfully",
                "user": {
                    "id": user["id"],
                    "email": user["email"],
                    "name": user["name"],
                    "role": user["role"],
                },
            }

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in_seconds,
        )

    except HTTPException:
        raise
    except RuntimeError as e:
        logger.exception(f"[{req_id}] Login infrastructure error: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is temporarily unavailable. Please try again later.",
        )
    except SQLAlchemyError:
        logger.exception(f"[{req_id}] Login DB error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service temporarily unavailable",
        )
    except DomainError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        logger.exception(f"[{req_id}] Login unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error during login",
        )


# ============================================================================
# POST /basic_auth/signup
# ============================================================================
@router.post(
    "/signup",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register new user",
    description="Create a new user account.",
)
async def signup(
    request: Request,
    user_data: User,
    db: AsyncSession = Depends(get_db),
):
    """Register a new user."""
    req_id = getattr(request.state, "request_id", "-")
    try:
        user = await AuthService(db).register_user(user_data)
        logger.info(f"[{req_id}] New user registered: {user.email}")
        return UserResponse.model_validate(user)

    except HTTPException:
        raise
    except DuplicateEmailError:
        # Race condition: another request created the email between
        # our exists_by_email check and the insert.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )
    except SQLAlchemyError:
        logger.exception(f"[{req_id}] Signup DB error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service temporarily unavailable",
        )
    except DomainError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception:
        logger.exception(f"[{req_id}] Signup unexpected error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Registration failed",
        )


# ============================================================================
# POST /basic_auth/logout
# ============================================================================
 

@router.post(
    "/logout",
    status_code=status.HTTP_200_OK,
    summary="Logout",
    description="Revoke refresh + access tokens and clear cookies. Idempotent.",
)
async def logout(
    request: Request,
    res: Response,
    refresh_token_body: Optional[str] = Body(None, embed=True, alias="refresh_token"),
    cookie_auth: Optional[str] = Depends(cookie_scheme),
    refresh_auth: Optional[str] = Depends(refresh_cookie_scheme),
    bearer: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
):
    req_id = getattr(request.state, "request_id", "-")

    # -------- 1. Pick the refresh token from body → cookie --------
    refresh_value = refresh_token_body or refresh_auth

    # -------- 2. Pick the access token from header → cookie --------
    access_value = bearer.credentials if bearer else cookie_auth

    revoked_any = False

    # -------- 3. Revoke refresh (kills rotation family too) --------
    if refresh_value:
        try:
            if await AuthService(db=None).revoke_session(refresh_value):
                revoked_any = True
                logger.info(f"[{req_id}] Revoked refresh token during logout")
        except Exception:
            logger.exception(f"[{req_id}] Logout refresh revocation failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Logout unavailable, please retry",
            )
    elif access_value:
        # Bearer client sent only the access token — no refresh token in
        # body or cookie. We still need to make sure their stored refresh
        # token can't mint new sessions. Log out everywhere for this user.
        payload = decode_jwt_ignore_expiry(access_value)
        user_id = payload.get("user_id") if payload else None
        if user_id:
            try:
                n = await AuthService(db=None).revoke_all_sessions_for_user(user_id)
                if n > 0:
                    revoked_any = True
                logger.info(
                    f"[{req_id}] Logout-everywhere for user {user_id} "
                    f"— revoked {n} refresh families"
                )
            except Exception:
                logger.exception(f"[{req_id}] Logout-everywhere failed")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Logout unavailable, please retry",
                )

    # -------- 4. Revoke access token jti (immediate kill) --------
    if access_value:
        payload = decode_jwt_ignore_expiry(access_value)
        if payload:
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti:
                ttl = max(int(exp - datetime.now(timezone.utc).timestamp()), 1) if exp else 3600
                try:
                    await token_store.blocklist_access(jti, ttl)
                    revoked_any = True
                    logger.info(f"[{req_id}] Blocklisted access token during logout")
                except Exception:
                    logger.exception(f"[{req_id}] Access blocklist failed (non-fatal)")

    # -------- 5. Clear cookies regardless --------
    if cookie_auth:
        res.delete_cookie(key="CSO", path="/", domain=None)
    if refresh_auth:
        res.delete_cookie(
            key="refresh_token",
            path="/app/v1/users/users_config/refresh",
            domain=None,
        )

    if not revoked_any:
        logger.info(f"[{req_id}] Logout called with no active tokens")
        return {"status": "success", "message": "Already logged out", "already_logged_out": True}

    logger.info(f"[{req_id}] User logged out")
    return {"status": "success", "message": "Logged out successfully"}