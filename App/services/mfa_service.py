import secrets
import pyotp
from typing import Optional, Literal

from App.core.settings import settings
from App.core.LoggingInit import get_core_logger
from App.core.exceptions import (
    DomainError, ValidationError, IncorrectPasswordError,
)

from App.core.mfa_crypto import encrypt_secret, decrypt_secret
from App.api.dependencies.auth import verify_password
from App.repository.UserRepository import UserRepository

logger = get_core_logger(__name__)

MFAMethod = Literal["totp", "email"]

# Enrollment secret staging (TOTP) — 10 min
_ENROLL_TTL = 600
# Email OTP purpose tags used by /email_otp/send
_PURPOSE_ENROLL = "mfa_enroll_email"
_PURPOSE_LOGIN = "login_2fa"
_PURPOSE_DISABLE = "mfa_disable"

def mask_email(email: str) -> str:
    """t***@example.com — never echo the full email back."""
    local, _, domain = email.partition("@")
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}***@{domain}"

class MFAService:
    """
    Business logic for MFA — supports both TOTP and Email OTP.

    Per-user choice is stored in `users.mfa_method`:
      - "totp"  → secret in DB, verify via pyotp
      - "email" → no secret, verify via Redis OTP from /email_otp/send
    """

    def __init__(self, db):
        self.db = db

    # ==================================================================
    # Enrollment
    # ==================================================================
 
 



    async def start_enrollment(self, user, method: MFAMethod = "totp") -> dict:
        """
        Begin MFA enrollment.

        - method="totp"  → generate secret, return otpauth URI for QR
        - method="email" → send 6-digit OTP to user.email (from DB),
                           return confirmation. Verify next via /enroll/verify.
        """
        from App.core.RedisConnector import redis_client

        if user.mfa_enabled:
            raise ValidationError("MFA is already enabled")

        if method == "totp":
            secret = pyotp.random_base32()
            uri = pyotp.TOTP(secret).provisioning_uri(
                name=user.email,
                issuer_name=settings.MFA_ISSUER_NAME,
            )
            c = await redis_client.ensure_connected()
            await c.set(f"mfa_enroll:{user.id}", secret, ex=_ENROLL_TTL)
            return {
                "method": "totp",
                "secret": secret,
                "otpauth_uri": uri,
                "issuer": settings.MFA_ISSUER_NAME,
                "expires_in": _ENROLL_TTL,
            }

        if method == "email":
            # Send OTP to the email already on file for this user.
            # The client NEVER supplies the email — we always use user.email.
            from App.store.otp_store import create_otp
            from App.core.email_sender import email_sender

            otp = await create_otp(user.email, "mfa_enroll_email")
            ttl_min = settings.EMAIL_OTP_TTL_SECONDS // 60

            sent = await email_sender.send(
                to=user.email,
                subject="Confirm email MFA",
                body=(
                    f"Hi,\n\n"
                    f"Your email MFA confirmation code is: {otp}\n\n"
                    f"This code expires in {ttl_min} minutes.\n\n"
                    f"If you didn't request this, ignore this email.\n"
                ),
                html=(
                    f"<html><body style='font-family:sans-serif;max-width:600px;"
                    f"margin:auto;padding:20px'>"
                    f"<h2>Confirm email MFA</h2>"
                    f"<div style='font-size:32px;font-weight:bold;letter-spacing:6px;"
                    f"background:#f4f4f4;padding:16px;text-align:center;"
                    f"border-radius:8px;margin:20px 0'>{otp}</div>"
                    f"<p>This code expires in {ttl_min} minutes.</p>"
                    f"<p style='color:#888;font-size:13px'>"
                    f"If you didn't request this, ignore this email.</p>"
                    f"</body></html>"
                ),
            )

            if not sent:
                # Send failed — surface it, don't silently succeed
                raise DomainError("Failed to send verification email")

            return {
                "method": "email",
                "message": f"Code sent to { mask_email(user.email)}",
                "expires_in": settings.EMAIL_OTP_TTL_SECONDS,
            }

        raise ValidationError(f"Unknown MFA method: {method}")

    async def confirm_enrollment(self, user, code: str, method: MFAMethod) -> dict:
        """
        Confirm enrollment by verifying the code the user provided.
        Persists mfa_enabled + mfa_method (+ secret for TOTP) on success.
        """
        from App.core.RedisConnector import redis_client

        if method == "totp":
            c = await redis_client.ensure_connected()
            secret = await c.get(f"mfa_enroll:{user.id}")
            if not secret:
                raise ValidationError("Enrollment expired — restart setup")
            if not pyotp.TOTP(secret).verify(code, valid_window=1):
                raise ValidationError("Invalid code — try again")

            target = await UserRepository(self.db).get_by_id(user.id)
            target.mfa_enabled = True
            target.mfa_method = "totp"
            target.mfa_secret_encrypted = encrypt_secret(secret)
            await self.db.flush()
            await c.delete(f"mfa_enroll:{user.id}")
            logger.info(f"MFA enrolled (totp) for user {user.id}")
            return {"status": "success", "mfa_enabled": True, "method": "totp"}

        if method == "email":
            from App.store.otp_store import verify_otp
            ok = await verify_otp(user.email, _PURPOSE_ENROLL, code)
            if not ok:
                raise ValidationError("Invalid or expired code")

            target = await UserRepository(self.db).get_by_id(user.id)
            target.mfa_enabled = True
            target.mfa_method = "email"
            target.mfa_secret_encrypted = None
            await self.db.flush()
            logger.info(f"MFA enrolled (email) for user {user.id}")
            return {"status": "success", "mfa_enabled": True, "method": "email"}

        raise ValidationError(f"Unknown MFA method: {method}")

    # ==================================================================
    # Verification — called from login_user
    # ==================================================================

    async def verify_for_login(self, user, code: str) -> bool:
        """
        Verify a code during login. Picks the method from user.mfa_method.
        - TOTP: pyotp against stored secret
        - Email: check against Redis OTP for purpose='login_2fa'
        """
        method = user.mfa_method or "totp"

        if method == "totp":
            if not user.mfa_secret_encrypted:
                return False
            secret = decrypt_secret(user.mfa_secret_encrypted)
            return pyotp.TOTP(secret).verify(code, valid_window=1)

        if method == "email":
            from App.store.otp_store import verify_otp
            return await verify_otp(user.email, _PURPOSE_LOGIN, code)

        return False

    # ==================================================================
    # Disable
    # ==================================================================

    async def disable(self, user, password: str, code: Optional[str]) -> dict:
        """Requires password + valid current code (TOTP or email)."""
        if not verify_password(password, user.password_hash):
            raise IncorrectPasswordError("Incorrect password")

        if not user.mfa_enabled:
            raise ValidationError("MFA is not enabled")

        if not code:
            raise ValidationError("Current code required to disable")

        method = user.mfa_method or "totp"

        if method == "totp":
            if not user.mfa_secret_encrypted:
                raise ValidationError("MFA secret missing — cannot verify")
            secret = decrypt_secret(user.mfa_secret_encrypted)
            if not pyotp.TOTP(secret).verify(code, valid_window=1):
                raise ValidationError("Invalid MFA code")

        elif method == "email":
            from App.store.otp_store import verify_otp
            if not await verify_otp(user.email, _PURPOSE_DISABLE, code):
                raise ValidationError("Invalid MFA code")

        else:
            raise ValidationError(f"Unknown MFA method: {method}")

        target = await UserRepository(self.db).get_by_id(user.id)
        target.mfa_enabled = False
        target.mfa_method = None
        target.mfa_secret_encrypted = None
        await self.db.flush()

        logger.info(f"MFA disabled for user {user.id}")
        return {"status": "success", "mfa_enabled": False}

    # ==================================================================
    # Send OTP — used by /auth/mfa/send-otp and any future flow
    # ==================================================================

    async def _generate_and_send_otp(
        self,
        user,
        purpose: str,
        subject: str,
        heading: str,
    ) -> bool:
        """
        Internal: generate an OTP, store it under (user.email, purpose),
        send the email. Returns True on success.

        Every public send_* method routes through here so the SMTP
        template lives in ONE place.
        """
        from App.store.otp_store import create_otp
        from App.core.email_sender import email_sender

        otp = await create_otp(user.email, purpose)
        ttl_min = settings.EMAIL_OTP_TTL_SECONDS // 60

        sent = await email_sender.send(
            to=user.email,
            subject=subject,
            body=(
                f"Hi,\n\n"
                f"{heading}: {otp}\n\n"
                f"This code expires in {ttl_min} minutes.\n\n"
                f"If you didn't request this, ignore this email.\n"
            ),
            html=(
                f"<html><body style='font-family:sans-serif;max-width:600px;"
                f"margin:auto;padding:20px'>"
                f"<h2>{heading}</h2>"
                f"<div style='font-size:32px;font-weight:bold;letter-spacing:6px;"
                f"background:#f4f4f4;padding:16px;text-align:center;"
                f"border-radius:8px;margin:20px 0'>{otp}</div>"
                f"<p>This code expires in {ttl_min} minutes.</p>"
                f"<p style='color:#888;font-size:13px'>"
                f"If you didn't request this, ignore this email.</p>"
                f"</body></html>"
            ),
        )

        if sent:
            logger.info(
                f"OTP email sent: user={user.id} email={mask_email(user.email)} "
                f"purpose={purpose}"
            )
        else:
            logger.error(
                f"OTP email FAILED: user={user.id} purpose={purpose}"
            )
        return sent

    async def send_login_otp(self, user) -> dict:
        """
        Send a login 2FA code to the user's email.

        Called from POST /auth/mfa/send-otp. Requires:
          - user.mfa_enabled == True
          - user.mfa_method == "email"

        Raises DomainError if email send fails.
        """
        if not user.mfa_enabled:
            raise ValidationError("MFA is not enabled for this account")

        method = user.mfa_method or "totp"
        if method != "email":
            raise ValidationError(
                "This account uses authenticator app, not email OTP"
            )

        if not settings.EMAIL_ENABLED:
            raise DomainError("Email service is disabled")

        sent = await self._generate_and_send_otp(
            user=user,
            purpose=_PURPOSE_LOGIN,
            subject="Your login code",
            heading="Your login code",
        )
        if not sent:
            raise DomainError("Failed to send login code — try again")

        return {
            "status": "sent",
            "method": "email",
            "message": f"Code sent to {mask_email(user.email)}",
            "expires_in": settings.EMAIL_OTP_TTL_SECONDS,
        }

    async def send_otp_email(self, user, purpose: str) -> dict:
        """
        Generic OTP sender for other flows (password reset, email change).

        Callers must:
          - validate the user exists and is active
          - ensure the purpose is on the allowed list (see EmailOTP.VALID_PURPOSES)

        This method does NOT validate the purpose string — it trusts the
        caller, because callers in this codebase are internal routes only.
        """
        if not settings.EMAIL_ENABLED:
            raise DomainError("Email service is disabled")

        subjects = {
            "password_reset": "Your password reset code",
            "email_change": "Confirm your email change",
            "verification": "Your verification code",
        }
        subject = subjects.get(purpose, "Your code")

        sent = await self._generate_and_send_otp(
            user=user,
            purpose=purpose,
            subject=subject,
            heading=subject,
        )
        if not sent:
            raise DomainError("Failed to send code — try again")

        return {
            "status": "sent",
            "message": f"Code sent to {mask_email(user.email)}",
            "expires_in": settings.EMAIL_OTP_TTL_SECONDS,
        }