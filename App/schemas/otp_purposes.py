from enum import Enum


class OTPPurpose(str, Enum):
    """Every place an OTP is keyed. Same value = same Redis bucket."""
    # MFA lifecycle
    MFA_ENROLL    = "mfa_enroll_email"
    MFA_LOGIN     = "login_2fa"
    MFA_DISABLE   = "mfa_disable"
    MFA_STEPUP    = "mfa_stepup"

    # Generic email OTP (used by /email_otp/*)
    VERIFICATION  = "verification"
    PASSWORD_RESET = "password_reset"
    EMAIL_CHANGE  = "email_change"

    # Admin & sensitive actions
    ENABLE_ACCOUNT   = "enable_account"
    DISABLE_ACCOUNT  = "disable_account"
    TEMP_TOKEN_MAKER = "temp_token_maker"
    RESET_AUTO_KILL  = "reset_auto_kill"
    UPDATE_PASSWORD  = "update_password"
    RESTORE_ACCOUNT  = "restore_account"