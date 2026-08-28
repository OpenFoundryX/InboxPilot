# InboxPilot

InboxPilot is an open-source, Gmail-native assistant for people who want fewer
interruptions rather than another inbox to manage. It batches non-urgent mail,
organizes each thread into one category, prepares replies on a schedule, answers
questions from mailbox context, and turns email and meetings into a daily plan.

> [!IMPORTANT]
> InboxPilot can read and modify a connected mailbox. Start with a test Google
> account, review the requested OAuth scopes, and understand the Mailman hold
> filter before using it with important mail.

## Why InboxPilot

Most email assistants react after a message has already reached the inbox and
triggered a notification. InboxPilot's optional Mailman mode installs a native
Gmail filter that holds non-VIP mail out of the inbox, then releases it during
delivery windows chosen by the user.

The rest of the product follows the same calm-workflow philosophy:

- **One category per thread:** To do, To follow up, Notification, FYI,
  Marketing, Noise, or a custom category.
- **Scheduled drafts:** replies are prepared in batches instead of appearing
  continuously as mail arrives.
- **Grounded mailbox search:** natural-language questions become several Gmail
  searches, with ranked results and surrounding thread context.
- **Daily briefings:** mail, meetings, reminders, deadlines, and follow-ups are
  combined into a useful plan.
- **Meeting capture:** use a Recall.ai bot, upload media, or record in the
  browser and receive a transcript and recap.
- **Gmail and Calendar remain the source of truth:** InboxPilot layers workflows
  on the tools users already have.

## Project status

InboxPilot is under active development and is not yet a stable `v1.0` release.
Database migrations and configuration may change between pre-1.0 versions.
Review [the open-source roadmap](docs/open-source-strategy.md) before relying on
it in a production organization.

The web application currently lives in the companion
[`OpenFoundryX/inboxos-web`](https://github.com/OpenFoundryX/inboxos-web)
repository. This repository contains the API, workers, integrations, migrations,
Docker development stack, and AWS deployment reference used by the hosted
service.

## Architecture

```text
Browser / web app
        |
        v
FastAPI API ---- PostgreSQL
    |     \------ Redis (cache, locks, results)
    |
    +---------- RabbitMQ ---------- Celery worker
                                      |
                                      +-- Gmail / Calendar
                                      +-- OpenAI
                                      +-- Recall.ai (optional)
                                      +-- S3 / R2 / MinIO (optional media)

Celery beat schedules polling, delivery windows, drafts, reminders,
meeting processing, retention, and daily routines.
```

See [Architecture](docs/architecture.md) for the module boundaries and main
data flows.

## Requirements

- Docker with Compose v2 (recommended), or Python 3.12 and
  [uv](https://docs.astral.sh/uv/)
- A Google Cloud OAuth client
- An OpenAI API key for AI features
- PostgreSQL 16, Redis 7, and RabbitMQ 3 when running without Docker
- Optional: Google Pub/Sub for low-latency Gmail events
- Optional: Recall.ai and S3-compatible storage for meeting features

## Quick start

1. Clone the repository and create local configuration:

   ```bash
   git clone https://github.com/OpenFoundryX/InboxPilot.git
   cd InboxPilot
   cp .env.example .env
   ```

2. At minimum, replace `JWT_SECRET`, configure PostgreSQL/broker values if the
   Compose defaults are unsuitable, and set `OPENAI_API_KEY`.

3. Start dependencies and services:

   ```bash
   make build
   make up
   ```

4. In another terminal, apply database migrations:

   ```bash
   make migrate
   ```

5. Open the API documentation at <http://localhost:8000/docs>. RabbitMQ's local
   management interface is at <http://localhost:15672>.

The backend can boot without Google, Recall, or billing credentials, but the
features backed by those providers will remain unavailable. A usable mailbox
connection requires the Google setup described in
[Self-hosting InboxPilot](docs/self-hosting.md).

## Google permissions

InboxPilot separates lightweight sign-in from the later mailbox connection. The
mailbox connection requests only the scopes used by the product:

| Scope | Why it is needed |
|---|---|
| `gmail.modify` | Read mail, apply/remove labels, archive, mark read, star, and create drafts |
| `gmail.settings.basic` | Install and remove the optional Mailman hold filter |
| `calendar.events` | Read and manage events used for scheduling and meeting workflows |
| `calendar.freebusy` | Offer booking times that are actually free |

These Gmail scopes are restricted Google scopes. A self-hosted OAuth app in
Google's Testing mode is limited to test users and its refresh tokens normally
expire after seven days. Publishing an OAuth app broadly requires Google's
verification process and may require a CASA assessment.

## Development

```bash
make install       # install Python and development dependencies with uv
make test          # run tests in the Compose test container
make lint          # Ruff linting
make typecheck     # mypy (known pre-1.0 errors remain)
make fmt           # format and autofix
make logs          # follow all Compose logs
make shell         # shell inside the API container
```

Run locally without Docker after starting PostgreSQL, Redis, and RabbitMQ:

```bash
uv sync --extra dev
PYTHONPATH=src uv run alembic upgrade head
PYTHONPATH=src uv run uvicorn main:app --reload
```

The test suite uses the configured PostgreSQL database and wraps each test in a
transaction that is rolled back. Do not point tests at a production database.

## Repository layout

```text
src/
  api/             FastAPI routers and dependencies
  core/            configuration, database, security, logging, locks
  integrations/    Google, meeting-bot, and storage provider boundaries
  models/          SQLAlchemy models
  schemas/         API request/response schemas
  services/        domain workflows and business rules
  workers/         Celery app and jobs
alembic/            database migrations
tests/              unit and integration tests
scripts/            operator diagnostics and invite management
docs/               architecture, runbooks, plans, and security material
infra/              AWS deployment used by the hosted environment
```

Some database names and Gmail labels still use the historical `inboxos` name.
They are retained for compatibility with existing installations and mailboxes.

## Optional meeting features

InboxPilot supports three paths into the same transcript and recap pipeline:

- Recall.ai bot for Zoom, Google Meet, or Microsoft Teams
- Direct browser recording
- Media upload

Recall keys are region-specific. Set `RECALL_API_BASE` to the region that issued
the key and configure the workspace webhook as:

```text
${PUBLIC_BASE_URL}/v1/webhooks/meeting-bot
```

Browser recordings and uploaded media use S3-compatible storage. The local
Compose stack provides MinIO on ports `9002` (API) and `9003` (console).

## Security

Read [SECURITY.md](SECURITY.md) before operating InboxPilot on real mailbox
data. Please report vulnerabilities privately through GitHub's private
vulnerability reporting; do not open a public issue containing exploit details,
tokens, email content, or personal data.

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md), follow
the [Code of Conduct](CODE_OF_CONDUCT.md), and use GitHub Discussions or an issue
for design questions before starting a large change.

## License

InboxPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE).
If you run a modified version as a network service, AGPL section 13 requires you
to offer the corresponding source code of that version to its users.

Copyright © 2026 OpenFoundryX and InboxPilot contributors.
