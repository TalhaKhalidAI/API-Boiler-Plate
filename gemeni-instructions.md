# GEMINI INSTRUCTIONS: FastAPI Auth System

## Context
You are assisting on a strict, layered FastAPI application that acts as an Identity Provider. The tech stack is Python, FastAPI, SQLAlchemy (Async), Postgres, and Redis.

## Strict MFA / 2FA Rules
The app supports two MFA methods. You MUST adhere to these rules:
1. **TOTP**: Secrets must be encrypted at rest using the `cryptography.fernet` module. You must implement an anti-replay guard using Redis (`mfa_used:{user_id}:{code}`).
2. **Email OTP**: 6-digit codes stored in Redis. You MUST use `secrets.compare_digest()` for comparison. Never use `==`. You must implement rate limiting (max 5 attempts).

## Architectural Boundaries
- **DO NOT** put SQL queries in API routes.
- **DO NOT** put `db.commit()` in Services. The request-scoped session handles it.
- **DO NOT** raise `HTTPException` in Services. Use custom exceptions like `ValidationError`, `MFARequiredError`, or `InfrastructureError`.
- **DO** use `App/core/RedisConnector.py` for all cache/state operations.

## Security Rules
- Redact PII (passwords, tokens, OTPs) from all logs and validation errors.
- If Redis fails during MFA, raise `InfrastructureError` (results in a 503 Service Unavailable), not a 401.

## Output Style
Provide clean, async Python code. Use type hints everywhere. Separate complex logic into helper functions rather than massive endpoints.