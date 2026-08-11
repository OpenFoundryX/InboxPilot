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
3. Connect Google (`/v1/integrations/google/connect`) — one grant covers Gmail
   and Calendar, and it is the same one the rest of the app uses.
4. `PUT /v1/meetings/settings` with `{"auto_join": true}`. It defaults **off**:
   recording other people is the user's call to make deliberately.

Meetings are booked by the `meetings.sweep` beat job a few minutes ahead of each
call, or on demand with `POST /v1/meetings/join` and a pasted link. Cost is
usage-based, roughly $0.65 per recorded hour at the time of writing.

### Capturing without a bot

Not every meeting is a call a bot can join. Two other ways in, both ending at the
same recap:

| Path | Endpoints |
|---|---|
| Record in the browser | `POST /v1/meetings/live`, then `POST /v1/meetings/{id}/uploads/complete` |
| Upload a file | `POST /v1/meetings/uploads`, then `POST /v1/meetings/{id}/uploads/complete` |

Both reserve a row, hand back a presigned S3 URL, and wait to be told the object
landed — which is verified against the bucket, not taken from the client. The
bytes go browser-to-bucket and never through this API.

`gpt-4o-transcribe` transcribes them (`services/meetings/transcribe.py`), because
Recall only transcribes calls its own bots attended. That model returns no speaker
labels, so summarization is told not to guess who committed to what. Duration is
read from the media with `ffprobe` and meters against the same bot-hour cap.

#### Media storage

`make up` runs MinIO and creates the bucket, so local development needs no cloud
account. The console is at http://localhost:9003 (credentials from `S3_*` in
`.env`, default `inboxos` / `inboxos-secret`).

The API port is published on **9002**, not MinIO's usual 9000, which is often
already taken by another project's MinIO. Override with `MINIO_API_PORT` and
`MINIO_CONSOLE_PORT` if that clashes too.

Two endpoint settings rather than one, because the browser and the API reach the
bucket at different addresses:

| Setting | Who uses it | Local value |
|---|---|---|
| `S3_ENDPOINT_URL` | API and worker, from inside the compose network | `http://minio:9000` |
| `S3_PUBLIC_ENDPOINT_URL` | The browser, following a presigned URL | `http://localhost:9002` |

A presigned URL's signature covers the host, so one signed for `minio:9000` is
rejected with `SignatureDoesNotMatch` the moment a browser opens it. Leave
`S3_PUBLIC_ENDPOINT_URL` blank for AWS and R2, where both sides use the same
public host.

For a real bucket: set `S3_*` to it, blank `S3_ENDPOINT_URL` for AWS or the
account endpoint for R2, and add a CORS rule allowing `PUT` and `GET` from the web
origin. A missing CORS rule fails in the browser with no server-side trace, so
it's the first thing to check if an upload dies at 0%. `ffmpeg` is already in the
Docker image.

## Common commands

```bash
make revision m="add users table"   # autogenerate migration
make migrate                         # upgrade head
make test
make lint
make fmt
```
