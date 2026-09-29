from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, Literal
from sqlalchemy.ext.asyncio import AsyncSession

from App.core.Connector import get_db
from App.core.settings import settings
from App.api.dependencies.auth import get_current_active_user
from App.repository.UserRepository import UserRepository
from App.services.mfa_service import MFAService
from App.models.otpModel import SendOTPRequest
from App.core.LoggingInit import get_core_logger

logger=get_core_logger(__name__)
mfa_router = APIRouter(prefix="/mfa", tags=["MFA"])


class StartEnrollRequest(BaseModel):
    method: Literal["totp", "email"] = "totp"


class VerifyEnrollRequest(BaseModel):
    method: Literal["totp", "email"] = "totp"
    code: str = Field(..., min_length=6, max_length=6)


class DisableRequest(BaseModel):
    password: str
    code: Optional[str] = None

@mfa_router.post("/enroll/start")
async def start_enroll(
    payload: StartEnrollRequest = StartEnrollRequest(),
    current_user: Dict[str, Any] = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    if not settings.ENABLE_MFA:
        raise HTTPException(403, "MFA is disabled")
    if payload.method == "email" and not settings.EMAIL_ENABLED:
        raise HTTPException(
            400,
            "Email MFA requires email service to be enabled",
        )
    if current_user.get("mfa_enabled"):
        raise HTTPException(400, "MFA is already enabled")
    user = await UserRepository(db).get_by_id(current_user["id"])
    return await MFAService(db).start_enrollment(user, method=payload.method)
@mfa_router.post("/enroll/verify")
async def confirm_enroll(
    payload: VerifyEnrollRequest,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    user = await UserRepository(db).get_by_id(current_user["id"])
    return await MFAService(db).confirm_enrollment(
        user, payload.code, method=payload.method,
    )


@mfa_router.post("/disable")
async def disable(
    payload: DisableRequest,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    user = await UserRepository(db).get_by_id(current_user["id"])
    return await MFAService(db).disable(user, payload.password, payload.code)


@mfa_router.get("/status")
async def status(
    current_user: Dict[str, Any] = Depends(get_current_active_user),
):
    return {
        "mfa_enabled": bool(current_user.get("mfa_enabled")),
        "mfa_method": current_user.get("mfa_method"),
    }

@mfa_router.post("/send-otp")
async def send_otp_for_login(
    payload: SendOTPRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Send a login OTP to the user's registered email.

    Pre-auth endpoint — no access token required. The email is looked
    up server-side by username/email. This endpoint is safe to call
    without authentication because:
      - it does NOT leak whether the account exists (same response shape)
      - rate-limited aggressively by middleware
    """
    from App.core.exceptions import DomainError

    # Don't leak whether the user exists.
    generic = {
        "status": "accepted",
        "message": "If this account uses email MFA, you'll receive a code",
    }

    repo = UserRepository(db)
    user = await repo.get_by_email(payload.username)
    if not user:
        user = await repo.get_by_name(payload.username)

    if not user or not user.mfa_enabled or (user.mfa_method or "totp") != "email":
        # Silently ignore — do not reveal which condition failed
        return generic

    if user.disabled or user.is_deleted:
        return generic

    try:
        await MFAService(db).send_login_otp(user)
    except DomainError:
        # Let real errors surface (SMTP failure) — but still not leak user info
        logger.exception(f"send-otp failed for user {user.id}")
        return generic

    return generic

@mfa_router.post("/send-disable-otp")
async def send_disable_otp(
    current_user: Dict[str, Any] = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Send an OTP to the current user's email so they can disable MFA.

    Only valid when:
      - MFA is enabled for the account
      - MFA method is "email"
    """
    if not settings.ENABLE_MFA:
        raise HTTPException(403, "MFA is disabled")

    if not settings.EMAIL_ENABLED:
        raise HTTPException(503, "Email service is disabled")

    user = await UserRepository(db).get_by_id(current_user["id"])
    if not user or not user.mfa_enabled:
        raise HTTPException(400, "MFA is not enabled")

    method = user.mfa_method or "totp"
    if method != "email":
        raise HTTPException(
            400,
            "Disable code is only for email-based MFA. "
            "Use your authenticator code.",
        )

    from App.core.exceptions import DomainError
    try:
        result = await MFAService(db).send_otp_email(user, purpose="mfa_disable")
    except DomainError as e:
        raise HTTPException(503, str(e))

    # Never echo the full email
    return {
        "status": "accepted",
        "message": result["message"],
        "expires_in": result["expires_in"],
    }