# App/core/redis_keys.py

def refresh_key(jti: str) -> str:
    return f"refresh:{jti}"


def revoked_rt_key(jti: str) -> str:
    return f"revoked_rt:{jti}"


def family_key(family_id: str) -> str:
    return f"refresh_family:{family_id}"


def blocklist_at_key(jti: str) -> str:
    return f"blocklist_at:{jti}"


def login_attempts_key(email: str, ip: str) -> str:
    return f"login_attempts:{email}:{ip}"

def user_families_key(user_id: int) -> str:
    return f"user_families:{user_id}"
