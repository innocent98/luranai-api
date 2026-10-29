from typing import Literal

from pydantic import EmailStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_postgres_driver(url: str) -> str:
    """Pin a bare ``postgresql://`` URL to the psycopg2 driver.

    This project ships ``psycopg2-binary``. SQLAlchemy 2.1 changed the default
    driver for a bare ``postgresql://`` URL to psycopg (v3), which is not
    installed, so a bare URL breaks under 2.1 while working under 2.0.
    Normalising to ``postgresql+psycopg2://`` keeps the driver deterministic
    across SQLAlchemy versions. Already-qualified URLs (``postgresql+<driver>://``)
    and non-Postgres URLs (sqlite, etc.) are left untouched.
    """
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://") :]
    return url


class Settings(BaseSettings):
    # Project Info
    PROJECT_NAME: str = "Luran"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str

    # Server
    SERVER_HOST: str
    SERVER_PORT: int = 8000
    FRONTEND_URL: str  # Frontend application URL for email links
    CUSTOMER_FRONTEND_URL: str | None = None  # B2C/customer application URL for email links
    FRONTEND_DOC_URL: str  # Documentation/marketing site URL

    # Security
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15  # Short-lived; refresh token handles longevity
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    ALGORITHM: str = "HS256"

    # Encryption at rest
    ENCRYPTION_KEY: str = ""
    ENCRYPTION_KEY_PREVIOUS: str = ""
    ENCRYPTION_MIGRATION_MODE: bool = True

    # CORS — configured exclusively via .env (comma-separated origins)
    BACKEND_CORS_ORIGINS: list[str] = []

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: str | list[str]) -> list[str] | str:
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",")]
        elif isinstance(v, list | str):
            return v
        raise ValueError(v)

    @field_validator("SECRET_KEY")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        """Ensure SECRET_KEY is secure."""
        dangerous_defaults = ["your_secret_key_here", "changeme", "secret"]
        if not v or v.strip() == "" or v in dangerous_defaults:
            raise ValueError("SECRET_KEY must be set. Generate using: openssl rand -hex 32")
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
            raise ValueError("OPENAI_API_KEY must start with 'sk-'. Check your API key.")
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
        dangerous = [
            "admin",
            "password",
            "changeme",
            "changethis",
            "12345678",
            "admin123",
            "password123",
            "luran",
            "luran123",
        ]
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

    @field_validator("DATABASE_URL")
    @classmethod
    def _pin_postgres_driver(cls, v: str) -> str:
        return normalize_postgres_driver(v)

    # Redis
    REDIS_URL: str

    # Agent Caller — escalation push (FCM HTTP v1)
    FIREBASE_CREDENTIALS_JSON: str = ""
    AGENT_ALERT_ENABLED: bool = True
    AGENT_ALERT_WAIT_SECONDS: int = 90
    AGENT_ALERT_ANDROID_CHANNEL_ID: str = "luran_escalation"

    # Qdrant Vector Database
    QDRANT_URL: str
    QDRANT_API_KEY: str | None = None
    QDRANT_COLLECTION_NAME: str

    # OpenAI
    OPENAI_API_KEY: str
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"  # 3K RPM, $0.00002/1K tokens
    OPENAI_LLM_MODEL: str = "gpt-6-luna"  # reasoning model; $0.10/1M in, $0.50/1M out
    # reasoning_effort for gpt-6/reasoning models: none|low|medium|high|xhigh.
    # Ignored for non-reasoning models (e.g. gpt-4o-mini). Tool-routing forces "none".
    OPENAI_REASONING_EFFORT: str = "low"

    EMBEDDING_DIMENSION: int = 1536  # text-embedding-3-small produces 1536-dim vectors

    # Email (optional)
    SMTP_TLS: bool = True
    SMTP_PORT: int = 587
    SMTP_HOST: str | None = None
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    EMAILS_FROM_EMAIL: EmailStr | None = None
    EMAILS_FROM_NAME: str | None = None

    # Admin
    FIRST_SUPERUSER_EMAIL: EmailStr
    FIRST_SUPERUSER_PASSWORD: str

    # Cloudinary (Image Upload Service)
    CLOUDINARY_CLOUD_NAME: str | None = None
    CLOUDINARY_API_KEY: str | None = None
    CLOUDINARY_API_SECRET: str | None = None
    CLOUDINARY_FOLDER: str = "luran/widget_logos"
    CLOUDINARY_PLATFORM_EMAIL_FOLDER: str = "luran/platform_email_images"
    CLOUDINARY_CHAT_FOLDER: str = "luran/chat_images"

    # Chat attachment retention (screen recording spec SR-D5) + video limits (SR-D10)
    ATTACHMENT_RETENTION_DAYS: int = 30
    ATTACHMENT_RETENTION_ENABLED: bool = True
    SCREEN_RECORDING_MAX_SECONDS: int = 60
    SCREEN_RECORDING_MAX_BYTES: int = 52_428_800

    # /chat/upload and /chat/video/* historically identified the customer by session_id
    # alone. A supplied user_identifier is always verified; flip this once every customer
    # client sends one to make it mandatory (see docs/sop/customer_credential_hardening.md).
    CHAT_MEDIA_REQUIRE_CUSTOMER_CREDENTIAL: bool = False

    # Paystack Payment Gateway
    PAYSTACK_SECRET_KEY: str
    PAYSTACK_PUBLIC_KEY: str | None = None
    PAYSTACK_PLAN_LITE_MONTHLY: str | None = None
    PAYSTACK_PLAN_LITE_ANNUAL: str | None = None
    PAYSTACK_PLAN_PROFESSIONAL_MONTHLY: str | None = None
    PAYSTACK_PLAN_PROFESSIONAL_ANNUAL: str | None = None
    PAYSTACK_PLAN_GROWTH_MONTHLY: str | None = None
    PAYSTACK_PLAN_GROWTH_ANNUAL: str | None = None
    # DEPRECATED: Paystack uses PAYSTACK_SECRET_KEY for webhook verification
    # See: https://paystack.com/docs/payments/webhooks/#verify-webhook-signature
    PAYSTACK_WEBHOOK_SECRET: str | None = None  # Not used - kept for backwards compatibility
    PAYSTACK_BASE_URL: str = "https://api.paystack.co"

    # Marketer referral program (see docs/superpowers/specs/2026-10-28-referral-program-design.md).
    # Reward/hold/window can be overridden at runtime by admins (referral_config table).
    # One reward per referred tenant, integer kobo (NGN).
    REFERRAL_REWARD_KOBO: int = 500_000
    # Days a reward waits after the first payment before it is payable.
    REFERRAL_HOLD_DAYS: int = 7
    # The first payment must land within this many days of signup.
    REFERRAL_COMMISSION_WINDOW_DAYS: int = 90
    # Lifetime of the first-party attribution cookie (luran_ref).
    REFERRAL_COOKIE_DAYS: int = 30
    # "manual": an admin pays outside and records the bank reference; "paystack": Paystack Transfers.
    REFERRAL_PAYOUT_PROVIDER: Literal["paystack", "manual"] = "manual"
    # Marketer links are <base>/register?ref=<code>.
    REFERRAL_LINK_BASE_URL: str = "https://luranai.com"

    # Backup Configuration (for database backups)
    ENABLE_CLOUD_BACKUP: bool = False
    CLOUD_PROVIDER: str | None = None
    GDRIVE_FOLDER: str | None = None
    RETENTION_DAYS: int = 30

    # Production Server
    API_WORKERS: int = 4
    LOG_LEVEL: str = "info"
    LOG_TO_FILE: bool = True
    LOG_FILE_PATH: str = "logs/app.log"
    LOG_FILE_ROTATION_MB: int = 10
    LOG_FILE_RETENTION_DAYS: int = 14
    LOG_FILE_COMPRESSION: str = "zip"

    # Database connection pooling enhancements
    DATABASE_POOL_RECYCLE: int = 3600
    DATABASE_POOL_PRE_PING: bool = True

    # Security Headers
    ENABLE_HSTS: bool = False

    # Rate limiter resiliency controls
    RATE_LIMIT_FAIL_OPEN: bool = True
    RATE_LIMIT_ERROR_LOG_COOLDOWN_SECONDS: int = 60

    # Subscription/trial reminder scheduling
    # Comma-separated day offsets before expiry. Example: "14,7,3,1,0"
    TRIAL_EXPIRY_REMINDER_DAYS: str = "14,7,3,1,0"
    # Comma-separated day offsets before paid-plan auto-renewal. Example: "7,3,1"
    SUBSCRIPTION_RENEWAL_REMINDER_DAYS: str = "7,3,1"
    # Optional comma-separated extra stopwords for rerank keyword overlap.
    RAG_EXTRA_STOPWORDS: str = ""
    # Optional explicit allowlist for automation outbound hosts (comma-separated).
    # Use with caution; empty by default for safer SSRF posture.
    AUTOMATION_OUTBOUND_HOST_ALLOWLIST: str = ""
    # Widget-install tracking ignores Luran's own origins (dashboard, preview, docs). The hosts of
    # FRONTEND_URL / CUSTOMER_FRONTEND_URL / FRONTEND_DOC_URL are always ignored; list extra
    # hostnames here, comma-separated. A leading "*." matches any subdomain.
    WIDGET_SEEN_IGNORED_HOSTS: str = ""

    # Account deletion (Google Play / GDPR right-to-erasure)
    # Cancellable grace period between a verified deletion request and purge.
    ACCOUNT_DELETION_GRACE_DAYS: int = 30
    # How long an emailed public verification link stays valid.
    ACCOUNT_DELETION_VERIFICATION_TOKEN_TTL_HOURS: int = 24

    # AI Website Auditor (Stage 4)
    # Public teaser scan: small page budget keeps it fast + token-frugal (only
    # a score + teaser gaps are shown; the full scan runs later in-dashboard).
    # Rate-limited to 3/day/IP, so per-visitor token exposure is tightly bounded.
    AUDITOR_PUBLIC_MAX_PAGES: int = 10
    AUDITOR_DASHBOARD_DEFAULT_MAX_PAGES: int = 500
    # Ceiling for plans whose page cap is NULL (= no plan limit, e.g. Enterprise).
    # "Unlimited" cannot mean unbounded here: crawl_site takes an int, and one
    # crawl holds one of AUDITOR_MAX_CONCURRENT_CRAWLS slots for its whole run.
    # A 50k-page site at ~2s/page would occupy half the auditor's capacity for a
    # day and starve every other tenant, so unlimited is bounded generously
    # rather than infinitely.
    AUDITOR_MAX_PAGES_CEILING: int = 5000
    AUDITOR_CLAIM_STALENESS_DAYS: int = 7
    AUDITOR_UNCLAIMED_PURGE_DAYS: int = 30
    # Fail audits stranded in QUEUED/CRAWLING/ANALYZING past this many minutes (a
    # worker killed mid-crawl can't fail its own report). Above any real crawl,
    # below ARQ's 1h job_timeout.
    AUDITOR_STALL_REAP_MINUTES: int = 45
    AUDITOR_PER_PAGE_TIMEOUT_S: int = 10
    # Bot-firewall soft-block (HTTP 403/429) retries. Some sites (Vercel BotID,
    # Cloudflare) challenge a fraction of headless requests; a few retries on the
    # SAME browser session (so the clearance cookie carries) recovers most pages.
    AUDITOR_SOFT_BLOCK_RETRIES: int = 2
    AUDITOR_SOFT_BLOCK_BACKOFF_S: list[int] = [1, 3]
    AUDITOR_CRAWL_RPS: int = 1
    # Concurrent Chromium instances across all audits in one worker. Bounded by
    # WORKER_MEM_LIMIT, not CPU: a browser measured 394 MiB, the worker baseline
    # is ~160 MiB, so N crawls need 160 + 394*N MiB. Default 2 = 948 MiB, which
    # fits the 1200m ceiling; 3 would be 1342 MiB and get OOM-killed. Raise this
    # only alongside WORKER_MEM_LIMIT.
    AUDITOR_MAX_CONCURRENT_CRAWLS: int = 2
    # Best-effort per-page hydration wait for SPAs; capped low because content
    # sites never reach network-idle (ads/trackers), so the full wait is wasted.
    AUDITOR_HYDRATION_SETTLE_MS: int = 1500
    AUDITOR_THIN_MIN_CONTENT_CHARS: int = 800
    # Real desktop-Chrome UA for the crawl context — the default headless UA
    # ("HeadlessChrome") is an obvious bot tell to some firewalls.
    AUDITOR_USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    # Render backend: "http" = self-hosted HTTP-first fetch (curl_cffi, Chrome TLS)
    # with per-page headless fallback (default, dev + prod — same path everywhere).
    # "local" = headless-only (debugging / explicit override / ops fallback).
    AUDITOR_RENDER_BACKEND: Literal["http", "local"] = "http"
    # Below this many extracted chars, the HTTP fetch is treated as a thin/SPA
    # shell and the page falls back to a headless render.
    AUDITOR_HTTP_MIN_CONTENT_CHARS: int = 500
    # curl_cffi browser-impersonation target (TLS/JA3 + browser-consistent headers).
    AUDITOR_HTTP_IMPERSONATE: str = "chrome"
    AUDITOR_HTTP_TIMEOUT_S: int = 15
    AUDITOR_MODEL_PROFILE: str = "gpt-4.1-mini"
    AUDITOR_MODEL_CLASSIFY: str = "gpt-4o-mini"
    AUDITOR_MODEL_GAPS: str = "gpt-4.1-mini"

    # --- Voice chat (ElevenLabs Agents) -------------------------------------
    ELEVENLABS_API_KEY: str | None = None
    ELEVENLABS_AGENT_ID: str | None = None
    ELEVENLABS_API_BASE: str = "https://api.elevenlabs.io"
    # Shared secret ElevenLabs presents back to us on custom-LLM / tool calls.
    VOICE_SHARED_SECRET: str | None = None
    # Opaque voice-session token lifetime. Short: it is minted per call start.
    VOICE_SESSION_TOKEN_TTL_SECONDS: int = 600
    # Platform-wide simultaneous voice sessions (EL plan cap ~20 Pro / ~40 Business).
    VOICE_MAX_CONCURRENT_SESSIONS: int = 20

    # --- WhatsApp channel (linked-device sidecar) ----------------------------
    # Global kill switch: off by default; a tenant additionally needs the
    # `can_use_whatsapp` capability granted by hand (platform admin).
    WHATSAPP_ENABLED: bool = False
    WHATSAPP_SIDECAR_URL: str = ""
    WHATSAPP_SIDECAR_TOKEN: str = ""
    WHATSAPP_SIDECAR_TIMEOUT_S: float = 10.0
    # Signed sidecar -> backend webhook. `_PREVIOUS` is accepted during rotation.
    WHATSAPP_WEBHOOK_SECRET: str = ""
    WHATSAPP_WEBHOOK_SECRET_PREVIOUS: str = ""
    # Pepper for the contact / phone HMAC lookup keys (NOT the unsalted
    # `hash_for_lookup`: phone numbers are low entropy and brute-forceable).
    WHATSAPP_CONTACT_PEPPER: str = ""
    # Platform-wide simultaneous linked sessions; checked before calling the
    # sidecar (the VPS is 8 GB total, per-session RAM beyond one is unmeasured).
    WHATSAPP_MAX_CONNECTIONS: int = 3
    # Owner typed from the phone/WhatsApp Web: AI stays silent this long.
    WHATSAPP_OWNER_PAUSE_MINUTES: int = 120
    # Images: the only hosts an outbound image URL may point at (comma separated; the sidecar
    # enforces its own MEDIA_URL_ALLOWLIST too) and the inbound byte cap (matches web chat).
    WHATSAPP_MEDIA_URL_HOSTS: str = "res.cloudinary.com"
    WHATSAPP_MEDIA_MAX_BYTES: int = 10 * 1024 * 1024

    def get_trial_expiry_reminder_days(self) -> list[int]:
        """Parse and validate reminder windows from env setting."""
        try:
            values = [
                int(item.strip())
                for item in self.TRIAL_EXPIRY_REMINDER_DAYS.split(",")
                if item.strip() != ""
            ]
        except ValueError as e:
            raise ValueError(
                "TRIAL_EXPIRY_REMINDER_DAYS must be a comma-separated list of integers"
            ) from e

        if not values:
            raise ValueError("TRIAL_EXPIRY_REMINDER_DAYS cannot be empty")
        if any(day < 0 for day in values):
            raise ValueError("TRIAL_EXPIRY_REMINDER_DAYS cannot contain negative values")

        return sorted(set(values))

    def get_subscription_renewal_reminder_days(self) -> list[int]:
        """Parse and validate paid-subscription renewal reminder windows."""
        try:
            values = [
                int(item.strip())
                for item in self.SUBSCRIPTION_RENEWAL_REMINDER_DAYS.split(",")
                if item.strip() != ""
            ]
        except ValueError as e:
            raise ValueError(
                "SUBSCRIPTION_RENEWAL_REMINDER_DAYS must be a comma-separated list of integers"
            ) from e

        if not values:
            raise ValueError("SUBSCRIPTION_RENEWAL_REMINDER_DAYS cannot be empty")
        if any(day < 0 for day in values):
            raise ValueError("SUBSCRIPTION_RENEWAL_REMINDER_DAYS cannot contain negative values")

        return sorted(set(values))

    def get_automation_outbound_host_allowlist(self) -> set[str]:
        """Parse outbound automation host allowlist."""
        return {
            item.strip().lower()
            for item in self.AUTOMATION_OUTBOUND_HOST_ALLOWLIST.split(",")
            if item.strip()
        }

    def get_widget_seen_ignored_hosts(self) -> set[str]:
        """Hostnames (or `*.suffix` patterns) whose widget loads are not customer installs."""
        from urllib.parse import urlparse

        hosts = {
            item.strip().lower()
            for item in self.WIDGET_SEEN_IGNORED_HOSTS.split(",")
            if item.strip()
        }
        for url in (self.FRONTEND_URL, self.CUSTOMER_FRONTEND_URL, self.FRONTEND_DOC_URL):
            host = urlparse(url).hostname if url else None
            if host:
                hosts.add(host.lower())
        return hosts

    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_file=".env",
        extra="ignore",  # Ignore extra fields in .env (like POSTGRES_*, PGADMIN_*)
    )


settings = Settings()  # type: ignore[call-arg]
