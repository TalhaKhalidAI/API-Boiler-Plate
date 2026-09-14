# App/api/dependencies/auth.py - OPTIMIZED VERSION WITH OAUTH2
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from argon2 import PasswordHasher, exceptions as argon2_exceptions
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from App.core.settings import settings
from App.core.Connector import get_db
from App.repository.UserRepository import UserRepository
from App.core.LoggingInit import get_core_logger
from fastapi.security import HTTPAuthorizationCredentials,APIKeyCookie 
from App.core import token_store
import uuid
# Initialize logger
logger = get_core_logger(__name__)

# Password hasher
pwd_context = PasswordHasher(
    memory_cost=settings.MEMORY_COST,
    parallelism=settings.PARALLELISM,
    hash_len=settings.HASH_LENGTH,
    salt_len=settings.SALT_LENGTH
)
DUMMY_PASSWORD_HASH = pwd_context.hash("timing-safe-dummy-password")

# Use OAuth2PasswordBearer for standard OAuth2 flows
oauth2_scheme =HTTPBearer(auto_error=False)
cookie_scheme=APIKeyCookie(name="CSO",auto_error=False)
refresh_cookie_scheme = APIKeyCookie(name="refresh_token", auto_error=False)
# ========== PASSWORD FUNCTIONS ==========

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password using Argon2"""
    try:
        return pwd_context.verify(hash=hashed_password, password=plain_password)
    except argon2_exceptions.VerifyMismatchError:
        return False
    except Exception as e:
        logger.exception(f"Password verification error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Password verification failed"
        )

def get_password_hash(password: str) -> str:
    """Hash password using Argon2"""
    return pwd_context.hash(password=password)

# ========== TOKEN FUNCTIONS ==========

def create_access_token(
    data: Dict[str, Any], 
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Create JWT access token"""
    try:
        to_encode = data.copy()
        
        expire = datetime.now(timezone.utc) + (
            expires_delta if expires_delta else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        )
        
        to_encode.update({
            "exp": expire,
            "iat": datetime.now(timezone.utc),
            "type": "access",
            "jti": str(uuid.uuid4()),      # ← NEW: needed for access blocklist
        })
        encoded_jwt = jwt.encode(
            to_encode, 
            settings.secret_key_str, 
            algorithm=settings.ALGORITHM
        )
        
        logger.debug(f"Created access token for user: {data.get('sub', 'unknown')}")
        return encoded_jwt
        
    except Exception as e:
        logger.exception(f"Token creation error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create access token"
        )

def decode_jwt(token: str) -> Optional[Dict[str, Any]]:
    """Decode and validate JWT token."""
    try:
        payload = jwt.decode(
            token,
            settings.secret_key_str,
            algorithms=[settings.ALGORITHM],
        )
        return payload
    except jwt.ExpiredSignatureError:
        logger.debug("Token expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.JWTError as e:
        logger.debug(f"JWT decode failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as e:
        logger.exception(f"Unexpected token decode error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service error",
        )

def decode_jwt_ignore_expiry(token: str) -> Optional[Dict[str, Any]]:
    """Decode JWT verifying signature only — used for cross-checking claims (e.g. user_id) even if expired."""
    try:
        payload = jwt.decode(
            token,
            settings.secret_key_str,
            algorithms=[settings.ALGORITHM],
            options={"verify_exp": False}
        )
        return payload
    except jwt.JWTError as e:
        logger.warning(f"JWT decode (ignore-expiry) failed: {e}")
        return None

# ========== DEPENDENCY INJECTIONS ========== 
async def get_current_user_slt(

    cookie_auth:Optional[str]=Depends(cookie_scheme),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """
    Get current authenticated user from token.
    
    ⚠️ TOKEN TYPES:
    - Normal ('type': 'access'): Standard auth, expires in 13 hours
    - Refresh ('type': 'refresh'): Rejected for auth (use /refresh endpoint)
    - SLT ('types': 'slts'): Short-Live Token (2 min)
        🔴 WARNING: SLT tokens BYPASS all status checks!
        ✅ Purpose: Account restoration, password reset
        ⏰ Expiry: 2 minutes
        🔒 Single-use recommended
    """
 
    token=None
    
    if credentials:
        token = credentials.credentials
        logger.debug("Using Bearer token")
    
    # ✅ Check cookie second
    elif cookie_auth:
        
        token = cookie_auth
        
        logger.debug("Using cookie token")
    
    
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authentication token found"
        )

    # ✅ Clean token (remove "Bearer " prefix if present)
    if token.startswith("Bearer "):
        token = token[7:]

    # Decode token
    payload = decode_jwt(token)
    
    if payload is None:
        logger.warning("Invalid or malformed token received")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or malformed token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    jti = payload.get("jti")
    if jti and await token_store.is_access_blocked(jti):
        logger.warning(f"Blocklisted SLT token used: jti={jti}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    
    is_slt_token = payload.get("types") == "slts"
    # Check token type - REJECT REFRESH TOKENS!
    token_type = payload.get("type")
    if token_type != "access":
        logger.warning(f"Non-access token used for authentication: type={token_type!r}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type. An access token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Check expiration
    exp = payload.get("exp")
    if exp is None:
        logger.warning("Token has no expiration time")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has no expiration",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    expiration_datetime = datetime.fromtimestamp(exp, timezone.utc)
    if expiration_datetime <= datetime.now(timezone.utc):
        logger.warning(f"Expired token used: expired at {expiration_datetime}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Get user info from token
    user_id = payload.get("user_id")
    if not user_id:
        logger.warning("Token missing user_id")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Get user from database using repository
    try:
        repo = UserRepository(db)
        user = await repo.get_by_id(user_id)
        
        if not user:
            logger.warning(f"User not found for ID: {user_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        
        # ✅ SLT tokens bypass ALL status checks
        if is_slt_token:
            pass  # ← SLT token: skip everything!
        
        # ✅ Normal tokens: check status
        else:
            if user.is_deleted:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
            
            if user.disabled or not user.is_active:
                logger.warning(f"Inactive user tried to authenticate: {user_id}")
                raise HTTPException(403, "Account is disabled or inactive. Contact admin.")
            
        logger.debug(f"Authenticated user: {user.email} (ID: {user.id})")
        
        return {
            "permissions":user.permissions,
            "id": user.id,
            "user_id": user.id, 
            "email": user.email,
            "name": user.name,
            "role": user.user_role,
            "is_active": user.is_active,
            "disabled": user.disabled,
            "token_type": payload.get("type"),
            "token_purpose": payload.get("purpose"),
            "types": payload.get("types")
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error getting current user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service error"
        )

async def get_current_user(

    cookie_auth:Optional[str]=Depends(cookie_scheme),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """
    Get current authenticated user from token.
    
    ⚠️ TOKEN TYPES:
    - Normal ('type': 'access'): Standard auth, expires in 13 hours
    - Refresh ('type': 'refresh'): Rejected for auth (use /refresh endpoint)
    - SLT ('types': 'slts'): Short-Live Token (2 min)
        🔴 WARNING: SLT tokens BYPASS all status checks!
        ✅ Purpose: Account restoration, password reset
        ⏰ Expiry: 2 minutes
        🔒 Single-use recommended
    """
 
    token=None
    
    if credentials:
        token = credentials.credentials
        logger.debug("Using Bearer token")
    
    # ✅ Check cookie second
    elif cookie_auth:
        
        token = cookie_auth
        
        logger.debug("Using cookie token")
    
    
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No authentication token found"
        )
    
    # ✅ Clean token (remove "Bearer " prefix if present)
    if token.startswith("Bearer "):
        token = token[7:]

    # Decode token
    payload = decode_jwt(token)
    
    if payload is None:
        logger.warning("Invalid or malformed token received")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or malformed token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    jti = payload.get("jti")                                    # ← ADD
    if jti and await token_store.is_access_blocked(jti):        # ← ADD
        logger.warning(f"Blocklisted access token used: jti={jti}")  # ← ADD
        raise HTTPException(                                    # ← ADD
            status_code=status.HTTP_401_UNAUTHORIZED,           # ← ADD
            detail="Token has been revoked",                    # ← ADD
            headers={"WWW-Authenticate": "Bearer"},             # ← ADD
        )   
    is_slt_token = payload.get("types") == "slts"
    # Check token type - REJECT REFRESH TOKENS
    token_type = payload.get("type")
    if token_type != "access":
        logger.warning(f"Non-access token used for authentication: type={token_type!r}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type. An access token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Check expiration
    exp = payload.get("exp")
    if exp is None:
        logger.warning("Token has no expiration time")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has no expiration",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    expiration_datetime = datetime.fromtimestamp(exp, timezone.utc)
    if expiration_datetime <= datetime.now(timezone.utc):
        logger.warning(f"Expired token used: expired at {expiration_datetime}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Get user info from token
    user_id = payload.get("user_id")
    if not user_id:
        logger.warning("Token missing user_id")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Get user from database using repository
    try:
        repo = UserRepository(db)
        user = await repo.get_by_id(user_id)
        
        if not user:
            logger.warning(f"User not found for ID: {user_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        
        # ✅ SLT tokens bypass ALL status checks
        if is_slt_token:
            raise HTTPException(status.HTTP_403_FORBIDDEN,"Short term token not allowed")
   
        # ✅ Normal tokens: check status
        else:
            if user.is_deleted:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
            
            if user.disabled or not user.is_active:
                logger.warning(f"Inactive user tried to authenticate: {user_id}")
                raise HTTPException(403, "Account is disabled or inactive. Contact admin.")
            
        logger.debug(f"Authenticated user: {user.email} (ID: {user.id})")
        
        return {
            "permissions":user.permissions,
            "id": user.id,
            "user_id": user.id, 
            "email": user.email,
            "name": user.name,
            "role": user.user_role,
            "is_active": user.is_active,
            "disabled": user.disabled,
            "token_type": payload.get("type"),
            "token_purpose": payload.get("purpose"),
            "types": payload.get("types")
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error getting current user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service error"
        )

async def get_current_active_user(
    current_user: Dict[str, Any] = Depends(get_current_user)
) -> Dict[str, Any]:
    """Check if current user is active"""
    if current_user.get("disabled") or not current_user.get("is_active"):
        logger.warning(f"Inactive user access attempted: {current_user.get('email')}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user"
        )
    return current_user

async def get_admin_user(
    current_user: Dict[str, Any] = Depends(get_current_active_user)
) -> Dict[str, Any]:
    """Check if current user is admin"""
    if current_user.get("role") != "admin":
        logger.warning(f"Non-admin user tried admin action: {current_user.get('email')}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions"
        )
    return current_user

# ========== HELPER FUNCTIONS ==========

from sqlalchemy.exc import SQLAlchemyError

async def authenticate_user(
    uname: str,
    password: str,
    db: AsyncSession
) -> Optional[Dict[str, Any]]:
    """
    Authenticate by name OR email.

    Infrastructure failures (SQLAlchemyError) propagate so get_db()
    can convert them to 503. Only genuine "user not found / wrong
    password / account inactive" outcomes return None.
    """
    repo = UserRepository(db)

    # Lookup — let DB errors propagate to get_db().
    # Be defensive against alternate repo implementations or test doubles
    # that expose only one of the lookup methods.
    email_lookup = getattr(repo, "get_by_email", None)
    name_lookup = getattr(repo, "get_by_name", None)

    if "@" in uname:
        if email_lookup is not None:
            user = await email_lookup(uname)
        elif name_lookup is not None:
            user = await name_lookup(uname)
        else:
            raise AttributeError("UserRepository does not implement get_by_email or get_by_name")
    else:
        if name_lookup is not None:
            user = await name_lookup(uname)
        elif email_lookup is not None:
            user = await email_lookup(uname)
        else:
            raise AttributeError("UserRepository does not implement get_by_name or get_by_email")

    # Timing-safe: run dummy hash even on missing user
    if not user:
        logger.debug(f"Authentication failed: user not found - {uname}")
        try:
            verify_password(password, DUMMY_PASSWORD_HASH)
        except Exception:
            logger.exception("Dummy password verify failed")
        return None

    # Password check — a real config error should surface, not be hidden
    try:
        if not verify_password(password, user.password_hash):
            logger.debug(f"Authentication failed: wrong password - {uname}")
            return None
    except HTTPException:
        raise
    except RuntimeError as e:
        logger.exception(f"Password verification infrastructure error for {uname}: {e}")
        raise HTTPException(503, "Authentication service is temporarily unavailable. Please try again later.")
    except Exception:
        logger.exception(f"Password verification infrastructure error for {uname}")
        raise HTTPException(500, "Authentication service error")

    if user.disabled or not user.is_active:
        logger.debug(f"Authentication failed: account disabled - {uname}")
        return None
    if user.is_deleted:
        logger.debug(f"Authentication failed: account deleted - {uname}")
        return None

    logger.info(f"User authenticated successfully: {uname}")
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.user_role,
    }
 
# ========== ADDITIONAL UTILITIES ==========

def validate_password_strength(password: str) -> bool:
    """Validate password strength with comprehensive checks"""
    if len(password) < 8:
        return False
    
    # Check for at least one uppercase letter
    if not any(c.isupper() for c in password):
        return False
    
    # Check for at least one lowercase letter
    if not any(c.islower() for c in password):
        return False
    
    # Check for at least one digit
    if not any(c.isdigit() for c in password):
        return False
    
    # Check for at least one special character
    special_chars = '!@#$%^&*()_+-=[]{}|;:,.<>?`~'
    if not any(c in special_chars for c in password):
        return False
    
    return True

def create_refresh_token(data: Dict[str, Any],family_id:str) -> str:
    """Create refresh token (longer expiry)"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=7)  # 7 days
    
    to_encode.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "refresh",
        "jti":str(uuid.uuid4()),
        "family": family_id,
        
    })
    
    return jwt.encode(
        to_encode, 
        settings.secret_key_str, 
        algorithm=settings.ALGORITHM
    )

# async def refresh_access_token(refresh_token: str, db: AsyncSession) -> Optional[str]:
#     """Refresh access token using refresh token"""
#     try:
#         payload = decode_jwt(refresh_token)
#         if not payload or payload.get("type") != "refresh":
#             return None
        
#         user_id = payload.get("user_id")
#         if not user_id:
#             return None
        
#         repo = UserRepository(db)
#         user = await repo.get_by_id(user_id)
        
#         if not user or user.disabled or not user.is_active:
#             return None
        
#         # Create new access token
#         new_access_token = create_access_token({
#             "sub": user.email,
#             "user_id": user.id,
#             "name": user.name,
#             "role": user.user_role
#         })
        
#         return new_access_token
        
#     except Exception as e:
#         logger.exception(f"Token refresh error: {e}")
#         return None

async def issue_refresh_token(
    user_id: int,
    email: str,
    family_id: str,
) -> str:
    """Mint + store a refresh token. Single place for both."""
    refresh_token = create_refresh_token(
        data={"sub": email, "user_id": user_id},
        family_id=family_id,
    )

    payload = jwt.decode(
        refresh_token,
        settings.secret_key_str,
        algorithms=[settings.ALGORITHM],
    )
    jti = payload["jti"]

    await token_store.store_refresh(
        jti=jti,
        user_id=user_id,
        family_id=family_id,
        ttl=7 * 24 * 3600,
    )
    await token_store.track_family_for_user(
        user_id=user_id,
        family_id=family_id,
        ttl=7 * 24 * 3600,
    )

    return refresh_token

async def refresh_access_token(
    refresh_token: str,
    db: AsyncSession,
) -> Optional[Dict[str, str]]:
    """
    Validate + rotate refresh token.
    Returns {access_token, refresh_token} or None.
    """
    try:
        payload = decode_jwt(refresh_token)
    except HTTPException:
        logger.debug("Refresh token invalid or expired")
        return None

    if payload.get("type") != "refresh":
        return None

    jti = payload.get("jti")
    family_id = payload.get("family")
    if not jti:
        return None

    # Reuse detection — if already revoked, kill the whole family
    if await token_store.is_refresh_revoked(jti):
        logger.warning(f"Refresh reuse detected: jti={jti} family={family_id}")
        if family_id:
            await token_store.revoke_family(family_id)
        return None

    # Atomic consume — only one caller wins
    meta = await token_store.consume_refresh(jti)
    if not meta:
        return None

    user_id = meta.get("user_id")
    if not user_id:
        return None

    # 4. Load user
    repo = UserRepository(db)
    user = await repo.get_by_id(user_id)
    if not user or user.disabled or not user.is_active or user.is_deleted:
        return None

    # 5. Rotate — new access + new refresh, SAME family
    family = family_id or meta.get("family_id") or str(uuid.uuid4())

    new_access = create_access_token({
        "sub": user.email,
        "user_id": user.id,
        "name": user.name,
        "role": user.user_role,
    })

    new_refresh = create_refresh_token(
        data={"sub": user.email, "user_id": user.id},
        family_id=family,
    )

    # 6. Store new refresh in Redis
    new_jti = jwt.decode(
        new_refresh,
        settings.secret_key_str,
        algorithms=[settings.ALGORITHM],
    )["jti"]

    await token_store.store_refresh(
        jti=new_jti,
        user_id=user.id,
        family_id=family,
        ttl=7 * 24 * 3600,
    )

    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
    }
async def create_short_live_token(
    user_id: int,
    db: AsyncSession,
    purpose: str = "restore_account",
) -> Optional[str]:
    """
    Create a short-lived token (2 minutes) for a specific self-service action.
    """
    try:
        repo = UserRepository(db)
        user = await repo.get_by_id(user_id)

        if not user:
            return None

        short_token = create_access_token(
            data={
                "sub": user.email,
                "user_id": user.id,
                "name": user.name,
                "role": user.user_role,
                "types": "slts",
                "purpose": purpose,
                "jti": str(uuid.uuid4()),
            },
            expires_delta=timedelta(minutes=2),
        )

        return short_token

    except Exception as e:
        logger.exception(f"Short-live token creation error: {e}")
        return None

# Additional dependency for optional authentication
async def get_current_user_optional(
    token: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
) -> Optional[Dict[str, Any]]:
    """Optional authentication - returns user if authenticated, None otherwise"""
    if not token:
        return None

    try:
        return await get_current_user(credentials=token, db=db)
    except HTTPException:
        return None