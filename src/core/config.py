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
    # Echo every SQL statement. Deliberately separate from DEBUG: the console
    # log renderer is worth having on in development, a full query dump is not.
    SQL_ECHO: bool = False

    # API
    API_V1_PREFIX: str = "/v1"

    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL_MIN: int = 30
    REFRESH_TOKEN_TTL_DAYS: int = 30

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    # OAuth flows through the web app's /api proxy so session cookies land on
    # the :3000 origin. This value must match the Google console redirect URI.
    GOOGLE_REDIRECT_URI: str = "http://localhost:3000/api/auth/google/callback"
    # After login, send users to the in-app connect step (Gmail + Calendar).
    POST_LOGIN_REDIRECT_URL: str = "http://localhost:3000/onboarding/connect"
    # Base URL of the web app; used to return the browser to the app after a
    # Composio Gmail/Calendar grant.
    FRONTEND_BASE_URL: str = "http://localhost:3000"

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
    COMPOSIO_GMAIL_TOOLKIT_VERSION: str = "20260702_01"
    COMPOSIO_GCAL_AUTH_CONFIG_ID: str = ""
    COMPOSIO_GCAL_TOOLKIT_VERSION: str = "20260721_00"
    COMPOSIO_WEBHOOK_SECRET: str = ""

    # Must be publicly reachable for Composio webhooks (use ngrok/tunnel in local)
    PUBLIC_BASE_URL: str = "https://9d57-2401-4900-8813-6c30-87f-f481-54a0-d6e6.ngrok-free.app"
    MAILMAN_DEFAULT_TZ: str = "Asia/Kolkata"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
