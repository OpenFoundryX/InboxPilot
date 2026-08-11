"""Import every model module, so `Base.metadata` is always complete.

SQLAlchemy resolves a `ForeignKey`'s target table by *name*, against the shared
`Base.metadata`, at flush time rather than at import time. A process that
imported some model modules and not others therefore looks healthy right up to
its first flush, where sorting tables by dependency raises

    NoReferencedTableError: Foreign key associated with column
    'draft_settings.user_id' could not find table 'users'

The API and the Celery worker used to get away with it because their entry
points transitively import nearly everything. Anything narrower did not: running
a task on its own (`python -c "from workers.jobs.drafts_sweep import ..."`)
loads `models.drafts` and never `models.users`, so a sweep that found a due user
died inside the usage counter's insert — while a sweep that found nobody due
returned cleanly, having never flushed anything. A failure that depends on
whether the work list was empty is not one you want to debug twice.

Because Python imports a package before any of its submodules, listing them here
means `from models.anything import X` registers all of them. No call site has to
remember, which is the point — `alembic/env.py` kept this list by hand and had
already lost `drafts`, so the next `--autogenerate` would have proposed dropping
the draft tables.

Import order does not matter: FK targets are resolved lazily by name.
"""

from models import activity as activity  # noqa: F401
from models import auth as auth  # noqa: F401
from models import billing as billing  # noqa: F401
from models import categorization as categorization  # noqa: F401
from models import chat as chat  # noqa: F401
from models import drafts as drafts  # noqa: F401
from models import google as google  # noqa: F401
from models import mailman as mailman  # noqa: F401
from models import meetings as meetings  # noqa: F401
from models import notes as notes  # noqa: F401
from models import reminders as reminders  # noqa: F401
from models import routines as routines  # noqa: F401
from models import scheduling as scheduling  # noqa: F401
from models import users as users  # noqa: F401
