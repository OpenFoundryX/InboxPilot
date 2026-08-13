import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from core.config import settings

# Importing the package registers every model module on Base.metadata; the list
# lives in `models/__init__.py` so there is one of it. This used to be a hand-
# maintained copy here, and it had already fallen behind — `drafts` was missing,
# so the next `--autogenerate` would have read the draft tables as tables that
# should not exist and proposed dropping them.
import models  # noqa: F401
from models.base import Base

config = context.config

# `%` doubled because set_main_option writes into a configparser, which reads a
# lone `%` as interpolation syntax and raises before any migration runs. A
# percent-encoded character in the password is enough to trigger it: a generated
# RDS password containing `{` arrives here as `%7B` and alembic dies with
# "invalid interpolation syntax", naming a position in the URL and nothing about
# the cause. Only this call needs it — the URL passed to run_migrations_offline
# and the app's own engine never touch configparser.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
