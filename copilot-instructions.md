# Copilot Instructions for FastAPI Auth Boilerplate

## Tech Stack
- Python 3.10+, FastAPI, Async SQLAlchemy, Redis, Pydantic v2.

## Code Style
- Use `async def` for all endpoints, services, and repository methods.
- Use `typing.Optional` and type hints strictly.
- Use `secrets.compare_digest` for any token/OTP comparison (never `==`).

## Architecture Rules
- **Routes (`/api`)**: Should only call Services. No DB logic here.
- **Services (`/services`)**: Business logic only. Raise custom `DomainError` exceptions. **NEVER** call `db.commit()`.
- **Repository (`/repository`)**: SQLAlchemy queries only. Return ORM models.
- **Redis (`/store`)**: For all OTPs, rate limits, and blocklists.

## MFA Rules
- **TOTP**: Use `pyotp`. Encrypt secrets using `mfa_crypto.py`. Check Redis `mfa_used:{user_id}:{code}` to prevent replays.
- **Email OTP**: Generate via `secrets.randbelow(1_000_000)`. Store in Redis with TTL. Verify using `secrets.compare_digest()`.

## Error Handling
- Never throw `HTTPException` in a Service. Throw `DomainError` subclasses (e.g., `UserNotFoundError`, `MFARequiredError`).
- Wrap Redis calls in try/except and raise `InfrastructureError` if the connection drops.
- When raising validation errors, ensure PII (passwords, tokens) is redacted.