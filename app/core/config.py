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

    # Database
    DATABASE_URL: str
    DATABASE_POOL_SIZE: int = 5
    DATABASE_MAX_OVERFLOW: int = 10

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Qdrant Vector Database
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: Optional[str] = None
    QDRANT_COLLECTION_NAME: str = "luran_knowledge"

    # Google AI
    GOOGLE_API_KEY: Optional[str] = None
    EMBEDDING_MODEL: str = "models/gemini-embedding-001"  # gemini-embedding-1.0: 100 RPM, 1K RPD

    # AI Model Selection
    # PRODUCTION MODEL (Gemini Flash - Fast, cost-effective, high quality)
    GEMINI_MODEL: str = "models/gemini-2.0-flash-exp"  # Flash: 10 RPM free tier, 1M RPD | Cost: ~$0.075/1M input, ~$0.30/1M output

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

    class Config:
        case_sensitive = True
        env_file = ".env"


settings = Settings()
