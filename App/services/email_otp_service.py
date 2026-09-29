"""
Background email OTP sender. Runs as a FastAPI BackgroundTask so the
HTTP response returns immediately while SMTP does its work.
"""
from fastapi import BackgroundTasks

from App.core.email_sender import email_sender
from App.store.otp_store import create_otp
from App.core.settings import settings
from App.core.LoggingInit import get_core_logger

logger = get_core_logger(__name__)


async def send_otp_via_email(to: str, purpose: str = "verification") -> None:
    """
    Background task: generate OTP, send via SMTP.
    Failures are logged but never propagate — the HTTP response is already sent.
    """
    try:
        otp = await create_otp(to, purpose)

        subject = _subject_for(purpose)
        ttl_min = settings.EMAIL_OTP_TTL_SECONDS // 60

        body = (
            f"Hi,\n\n"
            f"Your {purpose.replace('_', ' ')} code is: {otp}\n\n"
            f"This code expires in {ttl_min} minutes.\n\n"
            f"If you didn't request this, ignore this email.\n"
        )

        html = (
            f"<html><body style='font-family:sans-serif;max-width:600px;margin:auto;padding:20px'>"
            f"<h2>Your {purpose.replace('_', ' ')} code</h2>"
            f"<div style='font-size:32px;font-weight:bold;letter-spacing:6px;"
            f"background:#f4f4f4;padding:16px;text-align:center;border-radius:8px;"
            f"margin:20px 0'>{otp}</div>"
            f"<p>This code expires in {ttl_min} minutes.</p>"
            f"<p style='color:#888;font-size:13px'>If you didn't request this, ignore this email.</p>"
            f"</body></html>"
        )

        await email_sender.send(to=to, subject=subject, body=body, html=html)
        logger.info(f"OTP email dispatched: {to} purpose={purpose}")

    except Exception:
        # Never raise — this is a background task after the response was sent.
        logger.exception(f"Failed to send OTP email to {to} purpose={purpose}")


def _subject_for(purpose: str) -> str:
    return {
        "verification": "Your verification code",
        "password_reset": "Your password reset code",
        "login_2fa": "Your login code",
        "email_change": "Your email change code",
    }.get(purpose, "Your code")