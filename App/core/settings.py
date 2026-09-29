# settings.py - PostgreSQL version
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr, Field, PostgresDsn, field_validator
from typing import Optional, Any
import os
from urllib.parse import quote_plus

from App.core.size_parser import parse_size


class Settings(BaseSettings):

 

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )
    MAX_LOGIN_ATTEMPTS:int=Field(...)
    COOKIE_SECURE: bool = Field(
        default=True,
        description="Whether to set the 'Secure' flag on cookies"
    )
    HTTPS_ONLY: bool = Field(
        default=True,
        description="Whether to enforce HTTPS-only connections"
    )
 
    ADMIN_EMAIL:str=Field(...)
    ADMIN_USERNAME:str=Field(...)
    ADMIN_PASSWORD:SecretStr=Field(...)
    # Security
    SECRET_KEY:SecretStr= Field(
        ...,
        min_length=32,
        description="Secret key for JWT token signing"
    )
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    ALGORITHM: str = Field(
        default="HS256",
        pattern="^(HS256|HS384|HS512|RS256|RS384|RS512|ES256|ES384|ES512|PS256|PS384|PS512)$"
    )
    MAX_DECOMPRESSED_BODY_SIZE: int = Field(
        default=4 * 1024 * 1024,
        ge=1,
        description="Maximum decompressed request body size in bytes",
    )
    ENABLE_GZIP:bool=Field(False,description="enable gzip ")

    GZIP_COMPRESS_LEVEL: int = Field(
        default=5, ge=1, le=9,
        description="gzip compression level (1=fastest, 9=smallest)"
    )
    
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(
        default=15,                          # ← also change this to 15
        ge=1,
        le=10080,
        description="Access token expiration time in minutes",
    )
    REFRESH_TOKEN_TTL_SECONDS: int = Field(
        default=7 * 24 * 3600,               # ← ADD THIS BACK
        ge=300,
        description="Refresh token lifetime in seconds",
    )
    MAX_BODY_SIZE: int = Field(
        default=1024 * 1024,
        ge=1,
        description="Maximum request body size in bytes"
    )
    # Advanced Production Features
    KILL_SWITCH_ENABLED: bool = Field(
        default=False,
        description="Global kill switch to disable the API (Maintenance Mode)"
    )
    
    RATE_LIMIT_DEFAULT: str = Field(
        default="100/minute",
        description="Default rate limit for all endpoints"
    )
    
    # PostgreSQL Configuration
    DATABASE_HOST: str = Field(
        default="localhost",
        description="PostgreSQL host"
    )
    
    DATABASE_PORT: str = Field(
        default="5432",
        pattern="^\d+$",
        description="PostgreSQL port"
    )
    
    DATABASE_USER: str = Field(
        default="postgres",
        description="PostgreSQL username"
    )
    
    DATABASE_PASSWORD: SecretStr = Field(
        default="",
        description="PostgreSQL password"
    )
    
    DATABASE_NAME: str = Field(
        default="myapp_db",
        description="PostgreSQL database name"
    )
    
    DATABASE_SCHEMA: str = Field(
        default="public",
        description="PostgreSQL schema"
    )
    
    # Async settings
    ASYNC_MODE: bool = Field(
        default=True,
        description="Enable async database operations"
    )
    
    # Connection Pool Settings
    DATABASE_POOL_SIZE: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Connection pool size"
    )
    
    DATABASE_MAX_OVERFLOW: int = Field(
        default=40,
        ge=0,
        description="Max overflow connections"
    )
    
    DATABASE_POOL_RECYCLE: int = Field(
        default=3600,
        ge=60,
        description="Connection recycle time in seconds"
    )
    DATABASE_POOL_TIMEOUT: int = Field(
        default=30,
        ge=1,
        description="Connection timeout in seconds"
    )
    
    DATABASE_ECHO: bool = Field(
        default=False,
        description="Enable SQL query logging"
    )
    
    # SSL Configuration
    DATABASE_SSLMODE: str = Field(
        default="prefer",
        pattern="^(disable|allow|prefer|require|verify-ca|verify-full)$",
        description="PostgreSQL SSL mode"
    )
    
    DATABASE_CONNECT_TIMEOUT: int = Field(
        default=10,
        ge=1,
        le=60,
        description="Connection timeout in seconds"
    )
    
    # Logging
    LOG_FILEPATH: str = Field(
        default="./logs/",
        description="Path to log files directory"
    )
    
    # Argon2 Hashing
    MEMORY_COST: int = Field(
        default=65536,
        ge=1024,
        le=131072,
        description="Memory cost for Argon2 hashing"
    )
    PARALLELISM: int = Field(
        default=2,
        ge=1,
        le=8,
        description="Parallelism factor for Argon2"
    )
    
    HASH_LENGTH: int = Field(
        default=32,
        ge=16,
        le=64,
        description="Hash length for Argon2"
    )
    SALT_LENGTH: int = Field(
        default=16,
        ge=8,
        le=64,
        description="Length of salt for password hashing"
    )

    # Email Configuration
    EMAIL_ENABLED: bool = Field(
        default=False,
        description="Enable/disable email sending globally"
    )
    EMAIL_SMTP_HOST: str = Field(
        default="localhost",
        description="SMTP server hostname"
    )
    EMAIL_SMTP_PORT: int = Field(
        default=1025,
        description="SMTP server port (587 for TLS, 465 for SSL, 1025 for Mailpit)"
    )
    EMAIL_SMTP_USERNAME: str = Field(
        default="",
        description="SMTP username (empty for local Mailpit)"
    )
    EMAIL_SMTP_PASSWORD: SecretStr = Field(
        default=SecretStr(""),
        description="SMTP password (app password for Gmail)"
    )
    EMAIL_FROM: str = Field(
        default="noreply@example.com",
        description="From email address"
    )
    EMAIL_FROM_NAME: str = Field(
        default="API Boilerplate",
        description="From display name"
    )
    EMAIL_USE_TLS: bool = Field(
        default=False,
        description="Use TLS (True for port 587, False for 1025/465)"
    )
    EMAIL_USE_SSL: bool = Field(
        default=False,
        description="Use SSL (True for port 465, False for 587/1025)"
    )
    EMAIL_TIMEOUT: int = Field(
        default=15,
        ge=5,
        le=60,
        description="SMTP timeout in seconds"
    )
    EMAIL_OTP_TTL_SECONDS: int = Field(
        default=300,
        ge=60,
        le=900,
        description="OTP validity in seconds (default 5 min)"
    )
    EMAIL_OTP_MAX_ATTEMPTS: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Max OTP verification attempts"
    )
    EMAIL_CA_BUNDLE: Optional[str] = Field(
    default="",
    description="Path to custom root CA bundle for SMTP TLS verification"
    )
    LISTEN_IP:Optional[str]=Field("0.0.0.0")
    PORT:int=Field(8002)
    AUTO_RELOAD:bool=Field(False)
    WORKER:int=Field(3)
#### MFA 
    ENABLE_MFA: bool = Field(
        default=False,
        description="Enable MFA login"
    )
    MFA_CHALLENGE_TTL_SECONDS: int = Field(
        default=300, ge=60, le=900,
        description="MFA challenge token TTL in seconds"
    )
    MFA_MAX_ATTEMPTS: int = Field(
        default=5, ge=1, le=10,
        description="Max code attempts per challenge"
    )
    MFA_ISSUER_NAME: str = Field(
        default="API Boilerplate",
        description="Issuer name shown in authenticator apps"
    )
    @field_validator('SECRET_KEY', mode='before')
    @classmethod
    def validate_secret_key(cls, v: Any) -> Any:
        """Ensure SECRET_KEY is set in production"""
        env = os.getenv("ENVIRONMENT", "development")
        if env == "production" and (v is None or v == ""):
            raise ValueError("SECRET_KEY must be set in production")
        return v  

    @field_validator('DATABASE_PASSWORD', mode='before')
    @classmethod
    def validate_password(cls, v: Any) -> Any:
        """Validate password is provided for production"""
        import os
        env = os.getenv("ENVIRONMENT", "development")
        
        if env == "production" and (v is None or v == ""):
            raise ValueError("DATABASE_PASSWORD must be set in production")
        
        return v
    
    @field_validator('MAX_BODY_SIZE', mode='before')
    @classmethod
    def validate_max_body_size(cls, v: Any) -> int:
        """Parse '1MB', '512KB', '2GB' to bytes."""
        if v is None:
            return 1024 * 1024
        if isinstance(v, str):
            return parse_size(v)
        if isinstance(v, int):
            if v <= 0:
                raise ValueError(f"MAX_BODY_SIZE must be positive, got {v}")
            return v
        raise ValueError(f"MAX_BODY_SIZE must be str or int, got {type(v)}")

    @field_validator('RATE_LIMIT_DEFAULT', mode='before')
    @classmethod
    def validate_rate_limit(cls, v: Any) -> Any:
        """Ensure RATE_LIMIT_DEFAULT is in a valid format (e.g., '100/minute')"""
        if v is None:
            return "100/minute"
        v_str = str(v).strip()
        if v_str.isdigit():
            # If user provided a raw number, default it to per minute
            return f"{v_str}/minute"
        if "/" not in v_str:
            # Fallback if no unit provided
            return f"{v_str}/minute"
        return v_str
    @property
    def database_url(self) -> str:
        """Get PostgreSQL database URL"""
        password = quote_plus(self.DATABASE_PASSWORD.get_secret_value())
        
        # Construct the URL
        url = (
            f"postgresql+asyncpg://"
            f"{self.DATABASE_USER}:{password}@"
            f"{self.DATABASE_HOST}:{self.DATABASE_PORT}/"
            f"{self.DATABASE_NAME}"
        )
        
        # Add optional parameters
        params = []
        if self.DATABASE_SCHEMA != "public":
            params.append(f"search_path={self.DATABASE_SCHEMA}")
        if self.DATABASE_SSLMODE != "prefer":
            params.append(f"sslmode={self.DATABASE_SSLMODE}")
        if self.DATABASE_CONNECT_TIMEOUT != 10:
            params.append(f"connect_timeout={self.DATABASE_CONNECT_TIMEOUT}")
        
        if params:
            url += "?" + "&".join(params)
        
        return url
    
    @property
    def sync_database_url(self) -> str:
        """Get sync PostgreSQL database URL (for Alembic)"""
        password = quote_plus(self.DATABASE_PASSWORD.get_secret_value())
        
        url = (
            f"postgresql://"
            f"{self.DATABASE_USER}:{password}@"
            f"{self.DATABASE_HOST}:{self.DATABASE_PORT}/"
            f"{self.DATABASE_NAME}"
        )
        
        return url
    
    # Computed properties
    @property
    def secret_key_str(self) -> str:
        """Get the secret key as string (use carefully)"""
        return self.SECRET_KEY.get_secret_value()
    
    def get_argon2_params(self) -> dict:
        """Get Argon2 parameters as a dictionary"""
        return {
            "memory_cost": self.MEMORY_COST,
            "parallelism": self.PARALLELISM,
            "hash_len": self.HASH_LENGTH
        }
    
    def get_connection_pool_params(self) -> dict:
        """Get connection pool parameters"""
        return {
            "pool_size": self.DATABASE_POOL_SIZE,
            "max_overflow": self.DATABASE_MAX_OVERFLOW,
            "pool_recycle": self.DATABASE_POOL_RECYCLE,
            "pool_timeout": self.DATABASE_POOL_TIMEOUT,
        }

settings = Settings()