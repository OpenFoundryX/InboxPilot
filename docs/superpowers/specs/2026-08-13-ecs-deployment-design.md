# Deploying InboxPilot to AWS ECS

**Date:** 2026-08-13
**Status:** Approved, ready for implementation planning

## Goal

Move the InboxPilot backend off Render and onto AWS ECS Fargate in `ap-south-1`,
defined entirely in Terraform, with a GitHub Actions pipeline that builds, runs
migrations, and rolls both services on every push to `main`.

## Scope

**In:** VPC and networking, ALB with an ACM certificate, an ECS cluster running
the API and the Celery worker, RDS Postgres, ElastiCache Redis, an S3 media
bucket, ECR, IAM, secrets in SSM Parameter Store, a CI/CD workflow, and a
first-deploy runbook.

**Out, deliberately:**

- **The frontend.** It stays on Vercel, which is what `FRONTEND_BASE_URL`
  already assumes and what Next.js is better served by. Nothing here prevents
  moving it later; it would be another task definition and a host-based ALB
  rule.
- **Migrating existing data.** This provisions a **fresh, empty RDS database**.
  No dump/restore off Render, no cutover window. Confirmed with the user.
- **Multi-AZ, autoscaling, and alarming.** Single-AZ RDS, one task per service,
  CloudWatch logs but no alarms. These are the first three things to add when
  there are paying customers who would notice an outage; none of them are
  structural changes to what is built here.
- **Retiring `render.yaml`.** It stays in the repo as the working deployment
  until the ECS stack is verified. Removing it is a separate decision.

## Decisions

| Decision | Choice |
|---|---|
| Compute | ECS Fargate, ARM64 (Graviton) |
| Region | ap-south-1 (Mumbai) |
| IaC | Terraform, in `infra/` |
| Cost posture | Lean — ~$84/mo |
| NAT gateway | None. Tasks run in public subnets with public IPs |
| Postgres | RDS `db.t4g.micro`, single-AZ, 20GB gp3 |
| Redis | ElastiCache `cache.t4g.micro` — **not** a container |
| Celery beat | Embedded in the worker (`-B`), one worker task |
| Migrations | One-shot ECS `RunTask` before services update |
| Secrets | SSM Parameter Store SecureStrings, not Secrets Manager |
| S3 auth | Dedicated IAM user with a static key, **not** the task role |
| CI/CD | GitHub Actions with OIDC, no long-lived AWS keys |

## Architecture

```
                  api.<domain>  (Route53 / external DNS)
                              │
                   ALB :443  ── ACM cert, ap-south-1
                              │  :8000
            ┌─────────────────┴──────────────────────┐
            │  ECS Fargate cluster  "inboxpilot"     │
            │                                        │
            │  service  api      1× 0.25vCPU/0.5GB   │  public subnets,
            │           uvicorn main:app             │  public IP, no NAT
            │                                        │
            │  service  worker   1× 0.5vCPU/2GB      │
            │           celery worker -B             │
            │                                        │
            │  task     migrate  one-shot RunTask    │
            │           alembic upgrade head         │
            └───────┬──────────────────┬─────────────┘
                    │                  │
        RDS Postgres 16          ElastiCache Redis 7
        db.t4g.micro             cache.t4g.micro
        ── private subnets, no internet route, no NAT ──

        S3 inboxpilot-media · ECR · SSM Params · CloudWatch Logs
```

One image, built once, runs all three roles. The task definitions differ only in
`command`, size, and which secrets they receive — exactly as `docker-compose.yml`
and `render.yaml` already arrange it.

### Networking, and why there is no NAT gateway

A NAT gateway costs ~$32/mo, which is 38% of this budget, and its only job would
be egress for tasks. Instead:

- **Two public subnets** (2 AZs — the ALB requires two) hold the ALB and both
  Fargate services. Tasks get `assign_public_ip = true`, which gives them direct
  egress to ECR, SSM, Google, OpenAI, Recall, and Razorpay.
- **Two private subnets** hold RDS and ElastiCache. Both are `publicly_accessible
  = false` and neither needs egress, so the absence of a NAT route costs them
  nothing.

"Public subnet" describes routing, not reachability. Inbound is governed by
security groups:

| Group | Inbound |
|---|---|
| `alb` | 80, 443 from `0.0.0.0/0` |
| `task` | 8000 from `alb` only |
| `rds` | 5432 from `task` only |
| `redis` | 6379 from `task` only |

### Beat is embedded, and ECS makes that sharper than Render did

The worker runs `celery -A worker.celery_app worker -B`, as on Render. This is
safe only while there is exactly one worker task — two embedded schedulers
double-fire every sweep.

ECS introduces a hazard Render does not have. A default rolling deployment
(`minimumHealthyPercent = 100`, `maximumPercent = 200`) briefly runs the old and
new task **simultaneously**, which is two beat schedulers, which is every sweep
fired twice on every deploy. The worker service therefore sets:

```
minimum_healthy_percent = 0
maximum_percent         = 100
```

so the old task stops before the new one starts. This leaves a gap of roughly a
minute with no worker; queued tasks wait in Redis and are consumed when the new
task comes up, which is the correct trade for a queue consumer.

Scaling past one worker means splitting beat into its own service at
`desired_count = 1`, and only then raising the worker count. The task definition
for that is a copy of the worker's with a different `command`.

### Migrations

`alembic upgrade head` runs as a **standalone task definition invoked with
`RunTask`**, before either service is updated, and the deploy aborts on a
non-zero exit.

This is deliberately different from `render.yaml`, which runs migrations from
the worker's start command because Render's free tier has no pre-deploy hook.
That arrangement cannot order the migration ahead of the API, so the API can
serve requests against a schema that has not been upgraded yet. ECS has no such
constraint.

The scheduling migration issues `CREATE EXTENSION btree_gist` for the
double-booking exclusion constraint. RDS Postgres ships `btree_gist` and the
master user holds `rds_superuser`, so this succeeds.

### Redis is managed, against the lean default

The lean posture would suggest Redis as a Fargate container at ~$9/mo versus
ElastiCache at ~$12. It is managed anyway, because Redis here is the **Celery
broker**, not just a cache. A container without durable storage loses the queue
on every restart and every deploy. Per the note already in `render.yaml`,
booking confirmations are queued rather than sent inline — so a lost queue is
accepted-and-never-delivered email. Three dollars a month is the wrong place to
economise.

For the same reason the worker does not run on `FARGATE_SPOT`, which would cut
its cost ~70%: `src/workers/celery_app.py` does not set `acks_late`, so a Spot
interruption drops in-flight tasks silently rather than redelivering them.
Enabling `acks_late` and moving the worker to Spot is a legitimate later
optimisation and is out of scope here.

### Secrets

Roughly 25 secrets live as SSM Parameter Store SecureStrings under
`/inboxpilot/prod/*`, referenced by ARN in each task definition's `secrets`
block, which ECS resolves into environment variables at task start.

Not Secrets Manager: it bills $0.40 per secret per month, about $10/mo, for
rotation and cross-account sharing that this stack does not use.

`scripts/push-secrets.sh` reads a local `.env` and writes the parameters.
Terraform consumes them as `data` sources, so **no secret value is ever written
to git or to Terraform state**. The script must run before the first
`terraform apply`, since a data source pointing at a missing parameter fails at
plan time. Non-secret configuration (`ENVIRONMENT`, `LOG_LEVEL`,
`GMAIL_POLL_ENABLED`, and similar) goes in the plain `environment` block.

### S3 uses a static key, not the task role

`src/integrations/storage/s3.py:36` raises `StorageError` when
`S3_ACCESS_KEY_ID` or `S3_SECRET_ACCESS_KEY` is blank, so boto3's automatic
fallback to the ECS task role never gets a chance to run. Rather than change
that, this design provisions a **dedicated IAM user scoped to the media bucket**
and stores its access key in SSM.

That is also the more robust choice regardless of the code. A presigned URL
cannot outlive the credentials that signed it, and ECS task-role credentials are
temporary and rotate on roughly a six-hour cycle —
`MEDIA_LIVE_URL_TTL_SECONDS=21600` is exactly six hours. Presigning with
task-role credentials would put live-media URLs right on the expiry boundary.
Static credentials make presign lifetimes predictable.

The bucket is private, with a CORS rule allowing `PUT` from the Vercel origin so
the browser can upload directly. Both `S3_ENDPOINT_URL` and
`S3_PUBLIC_ENDPOINT_URL` are set to empty strings in production so boto3
addresses real AWS; the guard at `s3.py:45` already refuses the half-configured
case where only the public one is set.

### Deploy pipeline

GitHub Actions on push to `main`, authenticating via OIDC against an IAM role —
no AWS access keys stored in GitHub.

1. `docker buildx build --platform linux/arm64`. The image is multi-arch
   already: `python:3.12-slim`, the `uv` binary, and apt's `ffmpeg` all publish
   arm64. Graviton Fargate is ~20% cheaper for the same work.
2. Push to ECR tagged with the git SHA. A lifecycle policy keeps the last 10.
3. `aws ecs run-task` the migrate task; wait for exit code 0 or fail the build.
4. Register new task definition revisions and update the `api` and `worker`
   services.
5. `aws ecs wait services-stable`; a service that does not stabilise fails the
   workflow rather than being reported green.

Images are tagged by SHA rather than `latest` so a rollback is a re-deploy of a
known revision instead of a rebuild.

## Cost

ap-south-1, monthly, at one task per service, at ARM/Graviton rates. These are
estimates to confirm against the AWS pricing calculator before committing.

| Component | Cost |
|---|---|
| ALB | ~$17 |
| Fargate ARM — api, 0.25 vCPU / 0.5 GB | ~$8 |
| Fargate ARM — worker, 0.5 vCPU / 2 GB | ~$20 |
| RDS db.t4g.micro + 20 GB gp3, single-AZ | ~$15 |
| ElastiCache cache.t4g.micro | ~$12 |
| S3, ECR, CloudWatch, data transfer | ~$5 |
| **Total** | **~$77** |

This is more than the equivalent Render plan. The justification is control and
AWS-ecosystem access, not cost, and that trade was made explicitly.

The worker is sized for `ffmpeg`. `MEDIA_UPLOAD_MAX_BYTES` is 1 GB, and
transcoding a file that size on 0.25 vCPU would take long enough to block the
queue. If media uploads turn out to be rare, dropping the worker to
0.25 vCPU / 1 GB saves ~$10/mo and is a one-line change. Fargate's default 20 GB
ephemeral volume is sufficient for a 1 GB download plus its transcode output.

## Cutover risks

These are properties of moving the deployment, not of the Terraform, and belong
in the runbook.

1. **`GOOGLE_TOKEN_ENCRYPTION_KEYS` must be carried over byte-for-byte.** A
   different key makes every stored Google OAuth token undecryptable, silently
   disconnecting every user's Gmail and Calendar.
2. **`PUBLIC_BASE_URL` changes, and four external consoles must be updated to
   match**: the Google OAuth authorised redirect URI, the Google Pub/Sub push
   endpoint, the Recall workspace webhook, and the Razorpay webhook. Missing one
   is silent — mail simply stops being processed, with nothing in the logs.
3. **`JWT_SECRET`** — carry it over, or every session is invalidated on cutover.
4. **DNS and the certificate.** ACM DNS validation must complete before the
   HTTPS listener works. Until `api.<domain>` resolves, the raw ALB hostname
   serves traffic but no external webhook should be pointed at it, or every URL
   above has to be changed twice.
5. **S3 CORS** must name the exact Vercel origin, including scheme, or browser
   uploads fail with an opaque CORS error rather than a useful one.

## Testing

Infrastructure is verified by exercising it, not by unit tests.

- `terraform validate` and `terraform plan` run in CI on pull requests touching
  `infra/`.
- **First-deploy verification, in the runbook:** `GET /health` through the ALB
  over HTTPS returns 200; `alembic current` in a one-off task reports head; a
  worker task log shows registered tasks and a beat tick; a Google account
  connects end to end; a presigned upload round-trips through the browser.
- **The double-beat check:** trigger a deploy and confirm from the worker logs
  that each scheduled sweep fires exactly once across the transition. This is
  the failure the deployment configuration exists to prevent, so it is the one
  worth confirming deliberately.

## Deliverables

- `infra/` — Terraform for VPC, ALB, ACM, ECS, RDS, ElastiCache, S3, ECR, IAM,
  SSM
- `scripts/push-secrets.sh` — populates SSM from a local `.env`
- `.github/workflows/deploy.yml` — build, migrate, deploy, verify
- `docs/runbooks/aws-deploy.md` — first deploy, the external-console updates,
  rollback, and common failures
