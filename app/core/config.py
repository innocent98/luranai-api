from typing import List, Optional
from pydantic import AnyHttpUrl, field_validator, EmailStr, ConfigDict
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Project Info
    PROJECT_NAME: str = "Luran"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str

    # Server
    SERVER_HOST: str
    SERVER_PORT: int = 8000
    FRONTEND_URL: str # Frontend application URL for email links
    FRONTEND_DOC_URL: str # Documentation/marketing site URL

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

    @field_validator("OPENAI_API_KEY")
    @classmethod
    def validate_openai_api_key(cls, v: str) -> str:
        """Ensure OPENAI_API_KEY is set."""
        if not v or v.strip() == "":
            raise ValueError(
                "OPENAI_API_KEY required. Get from: https://platform.openai.com/api-keys"
            )
        if not v.startswith("sk-"):
            raise ValueError(
                "OPENAI_API_KEY must start with 'sk-'. Check your API key."
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

    @field_validator("FIRST_SUPERUSER_PASSWORD")
    @classmethod
    def validate_platform_admin_password(cls, v: str) -> str:
        """Ensure platform admin initial password meets security requirements."""
        if not v or v.strip() == "":
            raise ValueError("Platform admin password must be set")

        if len(v) < 12:
            raise ValueError("Platform admin password must be at least 12 characters")

        # Reject common weak passwords
        dangerous = ["admin", "password", "changeme", "changethis", "12345678",
                     "admin123", "password123", "luran", "luran123"]
        if v.lower() in dangerous:
            raise ValueError("Platform admin password is too weak. Use a strong, unique password")

        # Require complexity (3 of 4: uppercase, lowercase, digit, special)
        has_upper = any(c.isupper() for c in v)
        has_lower = any(c.islower() for c in v)
        has_digit = any(c.isdigit() for c in v)
        has_special = any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in v)

        if sum([has_upper, has_lower, has_digit, has_special]) < 3:
            raise ValueError(
                "Password must contain 3 of: uppercase, lowercase, digits, special characters"
            )

        return v

    # Database
    DATABASE_URL: str
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 40

    # Redis
    REDIS_URL: str

    # Qdrant Vector Database
    QDRANT_URL: str
    QDRANT_API_KEY: Optional[str] = None
    QDRANT_COLLECTION_NAME: str

    # OpenAI
    OPENAI_API_KEY: str
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"  # 3K RPM, $0.00002/1K tokens
    OPENAI_LLM_MODEL: str = "gpt-4o-mini"  # 500 RPM, $0.150/1M input, $0.600/1M output

    EMBEDDING_DIMENSION: int = 1536  # text-embedding-3-small produces 1536-dim vectors

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

    model_config = ConfigDict(
        case_sensitive=True,
        env_file=".env",
        extra="ignore"  # Ignore extra fields in .env (like POSTGRES_*, PGADMIN_*)
    )


settings = Settings()
