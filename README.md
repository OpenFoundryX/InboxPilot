# myapp

A FastAPI modular monolith with async SQLAlchemy, Celery, and agentic AI workflows.

## Layout

```
src/app/
  main.py          # FastAPI app factory + lifespan
  celery_app.py    # Celery instance + config
  worker.py        # worker entrypoint (imports task modules)
  beat_schedule.py # Celery beat schedule
  core/            # config, database, security, logging, exceptions, redis
  domains/         # users, invoices, billing (models/schemas/router/service/repository/tasks)
  agents/          # runtime, tools, prompts, workflows, llm clients
  api/             # deps + aggregated /v1 router
  models/          # declarative Base + mixins
```

## Quickstart

```bash
cp .env.example .env
make build
make up            # api :8000, worker, beat, postgres, redis, rabbitmq
make migrate       # apply DB migrations
```

Then open http://localhost:8000/docs — and the RabbitMQ management UI at
http://localhost:15672 (user/pass from `.env`, default inboxos/inboxos).

## Local (without Docker)

```bash
uv sync --extra dev
PYTHONPATH=src uv run uvicorn main:app --reload   # needs Postgres, Redis + RabbitMQ running
```

## Meeting notetaker

A bot joins the user's Zoom / Google Meet / Teams calls, records and transcribes
them, and turns each call into a recap: a summary email, a stored transcript,
reminders for dated action items, and a section in the daily briefing.

[Recall.ai](https://recall.ai) supplies the bot and raw transcription; the join
rules, summarization, and delivery are ours. The vendor sits behind
`integrations/meetingbot/base.py`, so swapping it is a new module plus
`MEETING_BOT_PROVIDER`.

Setup:

1. Set `RECALL_API_KEY` and `RECALL_API_BASE` (the region host must match the key).
2. In the Recall dashboard, point the workspace webhook at
   `$PUBLIC_BASE_URL/v1/webhooks/meeting-bot` and copy the signing secret into
   `RECALL_WEBHOOK_SECRET`.
3. Connect Google Calendar (`/v1/integrations/calendar/connect`) — the same
   Composio grant the rest of the app uses.
4. `PUT /v1/meetings/settings` with `{"auto_join": true}`. It defaults **off**:
   recording other people is the user's call to make deliberately.

Meetings are booked by the `meetings.sweep` beat job a few minutes ahead of each
call, or on demand with `POST /v1/meetings/join` and a pasted link. Cost is
usage-based, roughly $0.65 per recorded hour at the time of writing.

## Common commands

```bash
make revision m="add users table"   # autogenerate migration
make migrate                         # upgrade head
make test
make lint
make fmt
```
