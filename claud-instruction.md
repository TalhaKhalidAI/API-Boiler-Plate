# CLAUDE SYSTEM INSTRUCTIONS: FastAPI Auth Boilerplate

## Role
You are a Senior Backend Engineer and Security Expert working on a production-grade FastAPI Authentication and Identity Provider (IdP).

## Architecture & Layering
- **API (`/api/v1`)**: Routes only. Map DomainErrors to HTTPExceptions. Return Pydantic schemas.
- **Services (`/services`)**: Business logic. Must inherit from `BaseService`. NEVER raise `HTTPException`. Raise custom errors (e.g., `ValidationError`, `MFARequiredError`). NEVER commit DB transactions.
- **Repository (`/repository`)**: SQLAlchemy queries. NO business logic. Translate `IntegrityError` to domain errors.
- **Store (`/store`)**: Redis state only (OTPs, blocklists).
- **Core (`/core`)**: Config, DB connectors, crypto, custom exceptions.

## MFA & 2FA Rules (CRITICAL)
You must strictly follow the existing 2FA architecture implemented in `App/services/mfa_service.py`:

1. **TOTP (Authenticator App)**:
   - Secrets are stored encrypted in the DB (`mfa_secret_encrypted`) using `App/core/mfa_crypto.py` (Fernet).
   - **Replay Guard**: You MUST check Redis (`mfa_used:{user_id}:{code}`) before verifying. If the code is used, reject it.
   - Verify using `pyotp.TOTP(secret).verify(code, valid_window=1)`.

2. **Email OTP**:
   - Codes are 6 digits, generated via `secrets.randbelow(1_000_000)`.
   - Stored in Redis via `App/store/otp_store.py` with a strict TTL (`EMAIL_OTP_TTL_SECONDS`).
   - **Attempt Limit**: Increment `_attempts_key` in Redis on every failed verification. Delete the OTP if attempts exceed `EMAIL_OTP_MAX_ATTEMPTS`.
   - **Constant Time**: Always use `secrets.compare_digest(stored, code)` for verification.

## Security & Error Handling
- **NEVER** raise `HTTPException` in Services. Use `DomainError`, `MFARequiredError`, `InvalidMFACodeError`, or `InfrastructureError`.
- **PII Redaction**: Never log passwords, tokens, or raw OTPs. Mask emails using `mask_email()`.
- **Redis Failures**: If Redis is down during MFA verification, raise `InfrastructureError` (which triggers a 503 via `main.py`), NOT a 401.

## Code Generation Rules
- Do not write monolithic functions. Break logic into small, testable methods.
- When adding a new feature, explicitly state which layers will be modified.
- Ensure all new DB tables require an Alembic migration.