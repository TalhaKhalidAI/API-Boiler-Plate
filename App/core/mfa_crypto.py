import base64
from cryptography.fernet import Fernet, InvalidToken

from App.core.settings import settings
from App.core.exceptions import DomainError


def _fernet() -> Fernet:
    """Derive a Fernet key from SECRET_KEY (32 url-safe base64 bytes)."""
    raw = settings.secret_key_str.encode("utf-8")
    key = base64.urlsafe_b64encode(raw[:32].ljust(32, b"\0"))
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise DomainError("MFA secret corrupted — cannot decrypt")