from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from api.router import api_router
from core.config import settings
from core.exceptions import register_exception_handlers
from core.logging import configure_logging, get_logger
from integrations.composio.triggers import ensure_webhook_subscription

WEB_DIR = Path(__file__).parent / "web"

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    configure_logging()
    # Point Composio's project webhook at this deployment. Cheap, idempotent,
    # and necessary on every boot because PUBLIC_BASE_URL is a rotating tunnel
    # in development. Never fatal: mail delivery is not worth a failed boot.
    try:
        ensure_webhook_subscription()
    except Exception:
        log.exception("composio.webhook_subscribe_failed")
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
