"""Every model module must be registered whenever any one of them is.

SQLAlchemy resolves a ForeignKey's target by name against the shared
`Base.metadata` at flush time, not at import time. So a process that imported
`models.drafts` but never `models.users` looks fine until the first flush of
any ORM object, at which point sorting tables by dependency raises

    NoReferencedTableError: Foreign key associated with column
    'draft_settings.user_id' could not find table 'users'

That is exactly what running a Celery task standalone did: `drafts_sweep`
imports `models.drafts` and nothing else, so a pass that actually found a due
user died inside `get_or_create_counter`'s insert — while a pass that found
nobody due returned cleanly, because it never flushed anything.

Registration therefore has to be a property of importing `models` at all,
which is what `models/__init__.py` now guarantees.
"""

import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parent.parent.parent / "src")

EXPECTED_TABLES = {
    "users",
    "draft_settings",
    "subscriptions",
    "usage_counters",
    "chat_conversations",
    "chat_messages",
    "reminders",
    "routines",
}


def _in_fresh_process(code: str) -> str:
    """Run `code` with only `src` on the path, so imports are not pre-warmed.

    pytest has already imported half the app by the time a test runs, which
    would mask precisely the bug this file is about.
    """
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": SRC},
    )
    if result.returncode != 0:
        raise AssertionError(f"subprocess failed:\n{result.stderr}")
    return result.stdout.strip()


def test_importing_one_model_module_registers_every_table():
    out = _in_fresh_process(
        "from models.drafts import DraftSettings\n"
        "from models.base import Base\n"
        "print(','.join(sorted(Base.metadata.tables)))\n"
    )
    registered = set(out.split(","))
    assert EXPECTED_TABLES <= registered, EXPECTED_TABLES - registered


def test_foreign_keys_resolve_after_a_single_model_import():
    """The failing operation itself: sorting tables by FK dependency."""
    out = _in_fresh_process(
        "from models.drafts import DraftSettings\n"
        "from models.base import Base\n"
        "from sqlalchemy.sql.ddl import sort_tables_and_constraints\n"
        "sort_tables_and_constraints(list(Base.metadata.tables.values()))\n"
        "print('resolved')\n"
    )
    assert out == "resolved"


def test_importing_a_worker_task_alone_resolves_foreign_keys():
    """The reported reproduction: `python -c 'from workers.jobs... import'`."""
    out = _in_fresh_process(
        "from workers.jobs.drafts_sweep import drafts_sweep\n"
        "from models.base import Base\n"
        "from sqlalchemy.sql.ddl import sort_tables_and_constraints\n"
        "sort_tables_and_constraints(list(Base.metadata.tables.values()))\n"
        "print('resolved')\n"
    )
    assert out == "resolved"


def test_alembic_sees_every_model_module():
    """A hand-maintained import list in env.py had already lost `drafts`.

    Autogenerate diffs `target_metadata` against the database, so a module
    missing from it reads as a table that should not exist — the next
    `--autogenerate` would have emitted DROP TABLE draft_settings.
    """
    import models
    from models.base import Base

    modules = {
        p.stem
        for p in (Path(models.__file__).parent).glob("*.py")
        if p.stem not in {"__init__", "base"}
    }
    out = _in_fresh_process(
        "import models\n"
        "from models.base import Base\n"
        "print(','.join(sorted(Base.metadata.tables)))\n"
    )
    registered = set(out.split(","))

    # Every module contributes at least one table, so an unregistered module
    # is one whose tables are entirely absent.
    assert len(registered) >= len(modules), (
        f"{len(modules)} model modules but only {len(registered)} tables registered"
    )
    assert EXPECTED_TABLES <= set(Base.metadata.tables) | registered
