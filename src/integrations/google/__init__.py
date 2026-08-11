"""Direct Gmail and Google Calendar access.

Replaces the Composio integration. One OAuth grant per user covers both
products; `credentials` holds the tokens, `client` makes the authorized calls,
and `gmail`/`calendar` expose the same function signatures the Composio modules
did so consumers change only their import line.

Everything here is synchronous and does blocking HTTP. Call from Celery tasks
directly, or from async code via `run_in_threadpool`.
"""
