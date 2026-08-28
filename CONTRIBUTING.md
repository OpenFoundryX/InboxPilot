# Contributing to InboxPilot

Thank you for helping make InboxPilot better. Email automation has unusually
high consequences: a bug can hide, alter, send, or delete a user's mail. Favor
small, reviewable changes and explicit safety behavior.

## Before opening a pull request

- Search existing issues and pull requests.
- Open an issue before a large feature, schema redesign, new provider, OAuth
  scope, prompt/tool change, or destructive mailbox action.
- Never include real email content, OAuth tokens, customer data, production
  identifiers, or unsanitized logs in issues, fixtures, screenshots, or commits.
- For vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of opening an
  issue.

## Development setup

Docker is the supported path:

```bash
cp .env.example .env
make build
make up
make migrate
make test
```

For local Python development:

```bash
uv sync --extra dev
PYTHONPATH=src uv run alembic upgrade head
PYTHONPATH=src uv run pytest
```

The test database is transactional, but it must still be a disposable development
database. Never run the test suite against production.

## Pull-request requirements

Before submitting:

```bash
uv run ruff format --check src tests
uv run ruff check src tests
uv run pytest
git diff --check
```

`uv run mypy src` currently reports known pre-1.0 typing debt. Run it when
working in a typed area and do not introduce new errors; making it a required
zero-error gate is tracked as release work.

Pull requests should:

- Explain the user problem and why the chosen seam is appropriate.
- Include tests for behavior changes and failure paths.
- Include a migration for every persisted-schema change.
- Preserve idempotency for webhook and Celery retry paths.
- Require explicit confirmation for new destructive or externally visible
  actions.
- Update `.env.example` and documentation for new settings.
- Avoid combining unrelated refactors with a behavior change.

## Code conventions

- Python 3.12, type annotations, Ruff formatting, and a 100-character line limit.
- Domain behavior belongs in `services/`; provider-specific API mechanics belong
  in `integrations/`; routers should remain thin.
- Keep blocking provider calls out of async request paths or run them in a
  threadpool.
- Use structured logging and avoid message bodies, tokens, secrets, and personal
  data in normal logs.
- Provider webhooks must authenticate before their body can trigger work.
- A retry must not duplicate drafts, replies, reminders, billing mutations, or
  mailbox changes.

## AI changes

Prompt and model changes are behavior changes. Include representative tests and
document:

- What input reaches the model.
- What structured contract is expected.
- How malformed or low-confidence output fails safely.
- Whether the change affects cost, latency, privacy, or provider compatibility.

Never add email-controlled text to a system prompt in a way that can override
tool permissions or confirmation requirements.

## Migrations

Create a migration with:

```bash
make revision m="short description"
```

Review generated SQL carefully. A public release must be forward-upgradable;
avoid destructive migrations without an explicit staged migration and recovery
plan.

## Commits and licensing

By contributing, you agree that your contribution is licensed under the
repository's GNU AGPL v3 license. Keep commits focused and use an imperative
summary such as `fix: preserve category on thread replies`.
