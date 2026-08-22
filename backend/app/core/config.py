# backend/app/core/config.py
import logging
import socket
from functools import lru_cache
from typing import Literal
from pathlib import Path

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@lru_cache
def resolve_service_host(host: str) -> str:
    """Return `host` if it resolves, otherwise fall back to localhost.

    The single shared .env uses Docker Compose service names ("postgres",
    "redis"), which only resolve inside the compose network. When the app is
    run directly on the host (uvicorn in a local venv), those names fail DNS
    lookup and every DB/Redis call raises, surfacing as an opaque HTTP 500.
    Compose publishes both ports to the host, so localhost is the correct
    equivalent there.
    """
    try:
        socket.getaddrinfo(host, None)
        return host
    except OSError:
        logger.warning(
            "Host %r does not resolve; falling back to 'localhost'. "
            "(Expected when running outside Docker Compose.)",
            host,
        )
        return "localhost"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    APP_NAME: str = "6DM Appointment Setter"
    APP_ENV: Literal["local", "development", "staging", "production"] = "development"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"
    
    # Public URL Twilio uses to reach this server (ngrok in dev, production domain in prod)
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    # --- Database Credentials & Dynamic URLs ---
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "sixdm_db"
    
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    @property
    def postgres_host(self) -> str:
        return resolve_service_host(self.POSTGRES_HOST)

    @property
    def DATABASE_URL_ASYNC(self) -> str:
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.postgres_host}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @property
    def DATABASE_URL_SYNC(self) -> str:
        return f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.postgres_host}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    # --- Redis ---
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None
    CALL_STATE_TTL_SECONDS: int = 3600  # 1 hour session memory for calls

    @property
    def redis_host(self) -> str:
        return resolve_service_host(self.REDIS_HOST)

    @property
    def REDIS_URL(self) -> str:
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.redis_host}:{self.REDIS_PORT}/{self.REDIS_DB}"

    # --- Twilio Telephony ---
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_PHONE_NUMBER: str = ""  # E.164 format, e.g. +15551234567
    TWILIO_VALIDATE_SIGNATURE: bool = False  # Set to True in Production
    # Call recording requires a paid Twilio account: trial accounts reject
    # `record` / `recording_status_callback` with "Invalid or disallowed
    # parameters provided". Leave off until the account is upgraded.
    TWILIO_ENABLE_RECORDING: bool = False
    DEFAULT_TWIML_VOICE: str = "alice"
    DEFAULT_TWIML_LANGUAGE: str = "en-US"

    # --- Grok / xAI Integration ---
    XAI_API_KEY: str = ""
    XAI_BASE_URL: str = "https://api.x.ai/v1"
    GROK_MODEL: str = "grok-2-latest"
    GROK_TEMPERATURE: float = 0.4
    GROK_MAX_TOKENS: int = 512
    GROK_TIMEOUT_SECONDS: float = 30.0
    
    # --- Auth / JWT ---
    SECRET_KEY: str = Field(default="CHANGE_ME_IN_PRODUCTION_32_CHAR_MIN")
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14

    # --- Scheduling ---
    DEFAULT_APPOINTMENT_DURATION_MINUTES: int = 30
    SALES_PRESENTATION_DURATION_MINUTES: int = 45

    # --- Booking adapters ---
    # Fallback calendar for spa tenants on `booking_provider=google_calendar`
    # that set no per-tenant calendar id in SpaAccount.booking_config.
    GOOGLE_CALENDAR_ID: str = ""
    # Dominic's calendar: the sole destination for 6DM outbound sales bookings.
    SALES_GOOGLE_CALENDAR_ID: str = ""

    @property
    def public_ws_base_url(self) -> str:
        """Translates PUBLIC_BASE_URL into wss:// / ws:// for Twilio Media Streams."""
        return self.PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()