# settings.py - PostgreSQL version
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr, Field, PostgresDsn, field_validator
from typing import Optional, Any
import os
from App.core.size_parser import parse_size


class Settings(BaseSettings):

 

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # MinIO / S3 Configuration
    MINIO_ENDPOINT: str = Field(
        default="localhost:9000",
        description="MinIO server endpoint (host:port, no scheme)",
    )
    MINIO_ACCESS_KEY: str = Field(
        default="",
        description="MinIO access key (the app user, NOT root)",
    )
    MINIO_SECRET_KEY: SecretStr = Field(
        default=SecretStr(""),
        description="MinIO secret key",
    )
    MINIO_SECURE: bool = Field(
        default=False,
        description="Use HTTPS when connecting to MinIO",
    )
    MINIO_DEFAULT_BUCKET: str = Field(
        default="uploads",
        description="Default bucket used when none is specified",
    )
    MINIO_PRESIGN_EXPIRY_SEC: int = Field(
        default=3600,
        ge=60,
        le=604800,  # 7 days max
        description="Presigned URL expiry in seconds",
    )
    MINIO_MAX_POOL_CONNECTIONS: int = Field(
        default=20,
        ge=1,
        le=200,
        description="Max threads used by the MinIO threadpool wrapper",
    )

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
    MINIO_CA_BUNDLE: Optional[str] = Field(
    default=None,
    description="Path to CA bundle for MinIO TLS verification",
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
        password = self.DATABASE_PASSWORD.get_secret_value()
        
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
        password = self.DATABASE_PASSWORD.get_secret_value()
        
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
    @field_validator("MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", mode="before")
    @classmethod
    def validate_minio_credentials(cls, v, info):
        """In production, MinIO credentials must be set."""
        env = os.getenv("ENVIRONMENT", "development")
        if env == "production":
            if v is None or v == "":
                raise ValueError(f"{info.field_name} must be set in production")
        return v

    @property
    def minio_secret_str(self) -> str:
        """Get MinIO secret as string (use carefully)."""
        return self.MINIO_SECRET_KEY.get_secret_value()
# Create singleton instance
settings = Settings()