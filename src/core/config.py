from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    APP_NAME: str = "inboxos"
    ENVIRONMENT: str = "local"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # API
    API_V1_PREFIX: str = "/v1"

    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL_MIN: int = 30
    REFRESH_TOKEN_TTL_DAYS: int = 30

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/v1/auth/google/callback"
    POST_LOGIN_REDIRECT_URL: str = "http://localhost:3000"

    DATABASE_URL: str = "postgresql+asyncpg://inboxos_user:inboxos_password@db:5432/inboxos"

    CELERY_BROKER_URL: str = "amqp://inboxos:inboxos@rabbitmq:5672//"

    REDIS_URL: str = "redis://redis:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"

    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"

    COMMANDS_LOOKBACK: str = "newer_than:2d"
    COMMANDS_MAX_PER_SWEEP: int = 10

    COMPOSIO_API_KEY: str = ""
    COMPOSIO_GMAIL_AUTH_CONFIG_ID: str = ""
    COMPOSIO_GMAIL_CALLBACK_URL: str = "http://localhost:8000/"
    COMPOSIO_GMAIL_TOOLKIT_VERSION: str = "20260702_01"
    COMPOSIO_GCAL_AUTH_CONFIG_ID: str = ""
    COMPOSIO_GCAL_TOOLKIT_VERSION: str = "20260721_00"
    COMPOSIO_WEBHOOK_SECRET: str = ""

    PUBLIC_BASE_URL: str = "http://localhost:8000"
    MAILMAN_DEFAULT_TZ: str = "Asia/Kolkata"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
