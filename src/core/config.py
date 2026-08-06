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

    GOOGLE_REDIRECT_URI: str = "http://localhost:3000/api/auth/google/callback"
    POST_LOGIN_REDIRECT_URL: str = "http://localhost:3000/onboarding/connect"

    FRONTEND_BASE_URL: str = "http://localhost:3000"

    DATABASE_URL: str = "postgresql+asyncpg://inboxos_user:inboxos_password@db:5432/inboxos"

    CELERY_BROKER_URL: str = "amqp://inboxos:inboxos@rabbitmq:5672//"

    REDIS_URL: str = "redis://redis:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"

    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    # Meeting summarization runs on its own model. `OPENAI_MODEL` is the cheap
    # workhorse for classification, where one label is the whole output; a
    # meeting recap is long-form structured writing over tens of thousands of
    # characters, and mini's flat, generic prose is exactly where it shows.
    # Once per meeting, so the cost difference is small against the bot-hour.
    SUMMARY_MODEL: str = "gpt-4o"

    CLASSIFIER_MODELS: str = "gpt-4o-mini,gpt-4o"

    COMMANDS_LOOKBACK: str = "newer_than:2d"
    COMMANDS_MAX_PER_SWEEP: int = 10

    COMPOSIO_API_KEY: str = ""
    COMPOSIO_GMAIL_AUTH_CONFIG_ID: str = ""
    COMPOSIO_GMAIL_TOOLKIT_VERSION: str = "20260702_01"
    COMPOSIO_GCAL_AUTH_CONFIG_ID: str = ""
    COMPOSIO_GCAL_TOOLKIT_VERSION: str = "20260721_00"
    COMPOSIO_WEBHOOK_SECRET: str = ""

    PUBLIC_BASE_URL: str = "https://9d57-2401-4900-8813-6c30-87f-f481-54a0-d6e6.ngrok-free.app"
    MAILMAN_DEFAULT_TZ: str = "Asia/Kolkata"


    MEETING_BOT_PROVIDER: str = "recall"
    RECALL_API_BASE: str = "https://us-east-1.recall.ai"
    RECALL_API_KEY: str = ""

    RECALL_WEBHOOK_SECRET: str = ""
    RECALL_TIMEOUT_SECONDS: float = 30.0

    # Media storage for recordings we hold ourselves — browser recordings and
    # uploaded files. Recall keeps its own bot recordings and hands out signed
    # links; these settings are only for the media that has nowhere else to live.
    # S3 and R2 are the same API: R2 needs S3_ENDPOINT_URL set to the account
    # endpoint, AWS leaves it blank.
    MEDIA_STORAGE_PROVIDER: str = "s3"
    S3_ENDPOINT_URL: str = ""
    # Where the *browser* reaches the bucket, when that differs from where the
    # API reaches it. A presigned URL's signature covers the host, so one signed
    # for an internal address is rejected when opened from anywhere else.
    # Needed for MinIO in Docker (`minio:9000` inside, `localhost:9000` out);
    # blank for AWS and R2, where both sides use the same public host.
    S3_PUBLIC_ENDPOINT_URL: str = ""
    S3_REGION: str = "auto"
    S3_BUCKET: str = ""
    S3_ACCESS_KEY_ID: str = ""
    S3_SECRET_ACCESS_KEY: str = ""
    # 1 GB, matching what the upload dialog promises. Enforced twice: declared
    # up front so an oversized file is refused before it is transferred, and
    # again in the presigned policy so a client that lies still fails at S3.
    MEDIA_UPLOAD_MAX_BYTES: int = 1024 * 1024 * 1024
    # Long enough to upload a 1 GB file on a slow connection; the playback links
    # signed with the same TTL are re-resolved per page view anyway.
    MEDIA_URL_TTL_SECONDS: int = 3600
    # A live recording's upload link is signed when recording starts and used
    # when it stops, so it has to outlive the meeting. An hour would strand
    # anyone whose call ran long, at the one moment where failing loses the
    # recording outright.
    MEDIA_LIVE_URL_TTL_SECONDS: int = 6 * 3600

    # Transcription for media Recall never saw. Its bots transcribe their own
    # calls; an uploaded file or a browser recording has to be transcribed here.
    TRANSCRIBE_MODEL: str = "gpt-4o-transcribe"


    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""
    RAZORPAY_PLAN_STARTER_MONTHLY: str = ""
    RAZORPAY_PLAN_STARTER_ANNUAL: str = ""
    RAZORPAY_PLAN_PRO_MONTHLY: str = ""
    RAZORPAY_PLAN_PRO_ANNUAL: str = ""
    TRIAL_DAYS: int = 7

    @property
    def allowed_classifier_models(self) -> set[str]:
        listed = {m.strip() for m in self.CLASSIFIER_MODELS.split(",") if m.strip()}
        return listed | {self.OPENAI_MODEL}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
