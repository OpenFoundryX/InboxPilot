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

## Common commands

```bash
make revision m="add users table"   # autogenerate migration
make migrate                         # upgrade head
make test
make lint
make fmt
```
