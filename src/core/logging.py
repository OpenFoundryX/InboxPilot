import logging

import structlog

from core.config import settings


def configure_logging() -> None:
    """Configure structlog + stdlib logging. Call once at startup."""
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    )

    # SQLAlchemy's `echo` attaches its own handler *and* the record propagates
    # to the root one, so every statement was logged twice. Pin the loggers here
    # so only SQL_ECHO controls them.
    sql_level = logging.INFO if settings.SQL_ECHO else logging.WARNING
    for name in ("sqlalchemy.engine", "sqlalchemy.pool", "sqlalchemy.orm"):
        sql_logger = logging.getLogger(name)
        sql_logger.setLevel(sql_level)
        sql_logger.propagate = False

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer()
            if not settings.DEBUG else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
