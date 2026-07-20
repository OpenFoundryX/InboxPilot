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

    # JWT access tokens + refresh sessions
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL_MIN: int = 30
    REFRESH_TOKEN_TTL_DAYS: int = 30

    # Google OAuth (PKCE)
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/v1/auth/google/callback"
    # Where to send the browser after a successful login (your frontend)
    POST_LOGIN_REDIRECT_URL: str = "http://localhost:3000"

    # Database (async SQLAlchemy URL, e.g. postgresql+asyncpg://...)
    DATABASE_URL: str = "postgresql+asyncpg://inboxos_user:inboxos_password@db:5432/inboxos"

    # Celery broker (RabbitMQ / AMQP)
    CELERY_BROKER_URL: str = "amqp://inboxos:inboxos@rabbitmq:5672//"

    # Redis: Celery result backend + app cache
    REDIS_URL: str = "redis://redis:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"

    # LLM providers
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""

    # Integrations
    COMPOSIO_API_KEY: str = ""
    # Gmail auth config created in the Composio dashboard (Toolkits → Gmail)
    COMPOSIO_GMAIL_AUTH_CONFIG_ID: str = ""
    # Where Composio sends the browser back after the Gmail OAuth grant
    COMPOSIO_GMAIL_CALLBACK_URL: str = "http://localhost:8000/"
    # Manual tool execution requires a pinned toolkit version ("latest" is not
    # allowed). Bump this to a newer version from the Composio dashboard as needed.
    COMPOSIO_GMAIL_TOOLKIT_VERSION: str = "20260702_01"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
