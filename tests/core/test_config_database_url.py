"""The database URL a managed platform gives us is not the one we can use.

Render's own binding injects `postgres://…`. The app and Alembic both drive
async and need `postgresql+asyncpg://`, so the value has to be normalised on
the way in rather than hand-edited into the dashboard.
"""

import pytest

from core.config import Settings


def url_for(value: str) -> str:
    return Settings(DATABASE_URL=value).DATABASE_URL


@pytest.mark.parametrize(
    "given",
    [
        "postgres://u:p@host:5432/db",  # what Render and Heroku hand out
        "postgresql://u:p@host:5432/db",  # what most dashboards show
    ],
)
def test_a_sync_url_is_rewritten_onto_asyncpg(given):
    assert url_for(given) == "postgresql+asyncpg://u:p@host:5432/db"


def test_an_async_url_is_left_alone():
    given = "postgresql+asyncpg://u:p@host:5432/db"
    assert url_for(given) == given


def test_credentials_and_query_string_survive():
    given = "postgres://user:p%40ss@host:5432/db?sslmode=require"
    assert url_for(given) == "postgresql+asyncpg://user:p%40ss@host:5432/db?sslmode=require"


def test_an_unrelated_scheme_is_untouched():
    """SQLite in a test harness must not be mangled into Postgres."""
    given = "sqlite+aiosqlite:///./test.db"
    assert url_for(given) == given
