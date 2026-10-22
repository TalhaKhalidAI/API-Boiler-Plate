from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, Literal
from sqlalchemy.ext.asyncio import AsyncSession

from App.core.Connector import get_db
from App.core.settings import settings
from App.api.dependencies.auth import get_current_active_user
from App.repository.UserRepository import UserRepository
from App.services.mfa_service import MFAService
from App.schemas.otp_purposes import OTPPurpose
from App.core.LoggingInit import get_core_logger

logger=get_core_logger(__name__)
mfa_router = APIRouter(tags=["MFA"])


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

@mfa_router.post("/send_genaric_otp")
@mfa_router.post("/send_generic_otp", include_in_schema=False)
async def send_generic_otp(
    purpose: OTPPurpose,
    current_user: Dict[str, Any] = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Send an OTP to the current user's email for a specific validated purpose.

    - Purpose is required (select from the dropdown).
    - For TOTP users: Authenticator app generates codes continuously; no email OTP is needed.
    - For Email MFA users: Sends a 6-digit code scoped to the selected purpose.
    """
    if not settings.ENABLE_MFA:
        raise HTTPException(403, "MFA is disabled")

    if not settings.EMAIL_ENABLED:
        raise HTTPException(503, "Email service is disabled")

    user = await UserRepository(db).get_by_id(current_user["id"])
    if not user:
        raise HTTPException(404, "User not found")

    method = user.mfa_method or "totp"

    # For actions requiring an active MFA session (disable, step-up, sensitive admin operations)
    sensitive_mfa_purposes = (
        OTPPurpose.MFA_DISABLE,
        OTPPurpose.MFA_STEPUP,
        OTPPurpose.ENABLE_ACCOUNT,
        OTPPurpose.DISABLE_ACCOUNT,
        OTPPurpose.TEMP_TOKEN_MAKER,
        OTPPurpose.RESET_AUTO_KILL,
        OTPPurpose.UPDATE_PASSWORD,
        OTPPurpose.RESTORE_ACCOUNT,
    )
    if purpose in sensitive_mfa_purposes:
        if not user.mfa_enabled:
            raise HTTPException(400, "MFA is not enabled on this account")
        if method == "totp":
            raise HTTPException(
                400,
                "This account uses an authenticator app (TOTP). "
                "Use the 6-digit code from your authenticator app directly.",
            )

    # For enrollment, user must not already have MFA enabled
    if purpose == OTPPurpose.MFA_ENROLL and user.mfa_enabled:
        raise HTTPException(400, "MFA is already enabled on this account")

    from App.core.exceptions import DomainError
    try:
        result = await MFAService(db).send_otp_email(user, purpose=purpose.value)
    except DomainError as e:
        raise HTTPException(503, str(e))

    return {
        "status": "accepted",
        "message": result["message"],
        "expires_in": result["expires_in"],
    }