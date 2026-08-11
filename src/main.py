from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from api.router import api_router
from core import crypto
from core.config import settings
from core.exceptions import register_exception_handlers
from core.logging import configure_logging, get_logger

WEB_DIR = Path(__file__).parent / "web"

log = get_logger(__name__)


def _warn_on_broken_google_config() -> None:
    """Say at boot what would otherwise only surface mid-consent.

    Without an encryption key the app looks entirely healthy — it serves, it
    signs users in, the consent screen appears and Google grants everything —
    and then the very last step, writing the tokens, throws. All the user sees
    is a connect button that never turns green, with the real reason buried in
    a traceback. Config that can only fail at the worst possible moment should
    announce itself at the earliest one instead.

    Deliberately a log line and not a hard failure: an API that refuses to boot
    takes down billing, scheduling and the dashboard over a setting only the
    connect flow needs.
    """
    if not crypto.is_configured():
        log.error(
            "google.encryption_key_missing",
            detail=(
                "GOOGLE_TOKEN_ENCRYPTION_KEYS is unset — connecting a Google "
                "account WILL fail at the final step. Generate a key with: "
                "python -c \"from cryptography.fernet import Fernet; "
                'print(Fernet.generate_key().decode())"'
            ),
        )

    if settings.GMAIL_PUSH_ENABLED and not settings.GOOGLE_PUBSUB_TOPIC:
        log.error(
            "gmail.push_topic_missing",
            detail=(
                "GMAIL_PUSH_ENABLED is on but GOOGLE_PUBSUB_TOPIC is unset — no "
                "watch can be installed, so push will never fire."
            ),
        )
    elif settings.GMAIL_PUSH_ENABLED and not settings.GOOGLE_PUBSUB_SA_EMAIL:
        log.warning(
            "gmail.push_unverified",
            detail=(
                "GOOGLE_PUBSUB_SA_EMAIL is unset — the push endpoint accepts "
                "unauthenticated requests, which lets anyone who finds it spend "
                "Gmail quota."
            ),
        )

    if not settings.GMAIL_POLL_ENABLED and not settings.GMAIL_PUSH_ENABLED:
        log.error(
            "gmail.no_mail_path",
            detail=(
                "Both GMAIL_POLL_ENABLED and GMAIL_PUSH_ENABLED are off — no "
                "mail will be classified, no slash command will run, and no "
                "draft will be written."
            ),
        )
    elif not settings.GMAIL_POLL_ENABLED:
        log.warning(
            "gmail.reconciliation_disabled",
            detail=(
                "GMAIL_POLL_ENABLED is off — push is best-effort and its watch "
                "expires every 7 days, so a dropped notification or a failed "
                "renewal will stop mail silently with no sweep to recover it."
            ),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    configure_logging()
    _warn_on_broken_google_config()
    # Nothing to register with anyone: mail now arrives by polling Gmail from
    # the worker, so this deployment no longer needs to be reachable from the
    # internet for mail to be processed.
    yield
    # Shutdown
    from core.database import engine

    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        debug=settings.DEBUG,
        lifespan=lifespan,
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    return app


app = create_app()
