from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from App.core.Connector import get_db
from App.core.settings import settings
from App.core.LoggingInit import get_core_logger
from App.store.otp_store import verify_otp
from App.repository.UserRepository import UserRepository
from App.services.email_otp_service import send_otp_via_email
from App.schemas.otp_purposes import OTPPurpose

logger = get_core_logger(__name__)

email_otp_router = APIRouter(prefix="/email_otp", tags=["Email OTP"])

# Derive the whitelist from the single source of truth
VALID_PURPOSES = {p.value for p in OTPPurpose}


class SendOTPRequest(BaseModel):
    email: EmailStr
    purpose: str = "verification"


class VerifyOTPRequest(BaseModel):
    email: EmailStr
    purpose: str = "verification"
    code: str


@email_otp_router.post(
    "/send",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send OTP via email",
    description=(
        "Send a 6-digit OTP to the given email. Runs in background; "
        "returns 202 immediately. If the email is not registered, returns "
        "202 anyway (does not leak account existence)."
    ),
)
async def send_otp(
    payload: SendOTPRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    if not settings.EMAIL_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email service is disabled",
        )

    if payload.purpose not in VALID_PURPOSES:
        raise HTTPException(400, f"Invalid purpose. Allowed: {sorted(VALID_PURPOSES)}")

    # Don't leak whether the email exists.
    generic = {
        "status": "accepted",
        "message": "If this email is registered, you'll receive a code shortly",
    }

    repo = UserRepository(db)
    user = await repo.get_by_email(str(payload.email))
    if not user:
        user = await repo.get_by_name(str(payload.email))

    if not user or user.disabled or user.is_deleted:
        logger.info(f"OTP requested for unknown/inactive email or username: {payload.email}")
        return generic

    background_tasks.add_task(
        send_otp_via_email,
        to=user.email,
        purpose=payload.purpose,
    )
    return generic


@email_otp_router.post(
    "/verify",
    summary="Verify an email OTP",
    description="Verify the 6-digit code. Consumes the OTP on success.",
)
async def verify_email_otp(
    payload: VerifyOTPRequest,
    db: AsyncSession = Depends(get_db),
):
    if payload.purpose not in VALID_PURPOSES:
        raise HTTPException(400, "Invalid purpose")

    repo = UserRepository(db)
    user = await repo.get_by_email(str(payload.email))
    if not user:
        user = await repo.get_by_name(str(payload.email))
    target_email = user.email if user else str(payload.email)

    ok = await verify_otp(target_email, payload.purpose, payload.code)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired code",
        )
    return {"status": "verified"}