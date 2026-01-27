from typing import List, Optional
from pydantic import AnyHttpUrl, field_validator, EmailStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Project Info
    PROJECT_NAME: str = "Luran"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str = "development"

    # Server
    SERVER_HOST: str = "http://localhost"
    SERVER_PORT: int = 8000

    # Security
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    ALGORITHM: str = "HS256"

    # CORS
    BACKEND_CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:3000",
        "http://localhost:8000",
    ]

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: str | List[str]) -> List[str] | str:
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",")]
        elif isinstance(v, (list, str)):
            return v
        raise ValueError(v)

    @field_validator("SECRET_KEY")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        """Ensure SECRET_KEY is secure."""
        dangerous_defaults = ["your_secret_key_here", "changeme", "secret"]
        if not v or v.strip() == "" or v in dangerous_defaults:
            raise ValueError(
                "SECRET_KEY must be set. Generate using: openssl rand -hex 32"
            )
        if len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        return v

    @field_validator("GOOGLE_API_KEY")
    @classmethod
    def validate_google_api_key(cls, v: str) -> str:
        """Ensure GOOGLE_API_KEY is set."""
        if not v or v.strip() == "":
            raise ValueError(
                "GOOGLE_API_KEY required. Get from: https://makersuite.google.com/app/apikey"
            )
        return v

    @field_validator("PAYSTACK_SECRET_KEY")
    @classmethod
    def validate_paystack_secret_key(cls, v: str) -> str:
        """Ensure PAYSTACK_SECRET_KEY is set."""
        if not v or v.strip() == "":
            raise ValueError(
                "PAYSTACK_SECRET_KEY required. Get from: https://dashboard.paystack.com/#/settings/developers"
            )
        if not v.startswith("sk_"):
            raise ValueError(
                "PAYSTACK_SECRET_KEY must start with 'sk_' (secret key). Use sk_test_ for testing."
            )
        return v

    # Database
    DATABASE_URL: str
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 40

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Qdrant Vector Database
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: Optional[str] = None
    QDRANT_COLLECTION_NAME: str = "luran_knowledge"

    # Google AI
    GOOGLE_API_KEY: str
    EMBEDDING_MODEL: str = "models/gemini-embedding-001"  # gemini-embedding-1.0: 100 RPM, 1K RPD

    # AI Model Selection
    # PRODUCTION MODEL (Gemini Flash - Fast, cost-effective, high quality)
    GEMINI_MODEL: str = "models/gemini-2.5-flash-lite"  # Flash: 10 RPM free tier, 1M RPD | Cost: ~$0.075/1M input, ~$0.30/1M output

    # DEVELOPMENT MODEL (Gemma - Free, open source)
    # GEMINI_MODEL: str = "models/gemma-3-12b-it"  # Gemma: 30 RPM, 14.4K RPD | Cost: FREE

    EMBEDDING_DIMENSION: int = 3072  # gemini-embedding-001 produces 3072-dim vectors

    # Email (optional)
    SMTP_TLS: bool = True
    SMTP_PORT: int = 587
    SMTP_HOST: Optional[str] = None
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    EMAILS_FROM_EMAIL: Optional[EmailStr] = None
    EMAILS_FROM_NAME: Optional[str] = None

    # Admin
    FIRST_SUPERUSER_EMAIL: EmailStr
    FIRST_SUPERUSER_PASSWORD: str

    # Cloudinary (Image Upload Service)
    CLOUDINARY_CLOUD_NAME: Optional[str] = None
    CLOUDINARY_API_KEY: Optional[str] = None
    CLOUDINARY_API_SECRET: Optional[str] = None
    CLOUDINARY_FOLDER: str = "luran/widget_logos"

    # Paystack Payment Gateway
    PAYSTACK_SECRET_KEY: str
    PAYSTACK_PUBLIC_KEY: Optional[str] = None
    # DEPRECATED: Paystack uses PAYSTACK_SECRET_KEY for webhook verification
    # See: https://paystack.com/docs/payments/webhooks/#verify-webhook-signature
    PAYSTACK_WEBHOOK_SECRET: Optional[str] = None  # Not used - kept for backwards compatibility
    PAYSTACK_BASE_URL: str = "https://api.paystack.co"

    # Backup Configuration (for database backups)
    ENABLE_CLOUD_BACKUP: bool = False
    CLOUD_PROVIDER: Optional[str] = None
    GDRIVE_FOLDER: Optional[str] = None
    RETENTION_DAYS: int = 30

    # Production Server
    API_WORKERS: int = 4
    LOG_LEVEL: str = "info"

    # Database connection pooling enhancements
    DATABASE_POOL_RECYCLE: int = 3600
    DATABASE_POOL_PRE_PING: bool = True

    # Security Headers
    ENABLE_HSTS: bool = False

    class Config:
        case_sensitive = True
        env_file = ".env"


settings = Settings()
