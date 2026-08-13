# AWS deploy runbook

The stack: an ALB in front of a Fargate API task, a single Fargate worker with
Celery beat embedded, RDS Postgres, ElastiCache Redis, and an S3 media bucket —
all in `ap-south-1`, all defined in `infra/`.

Design rationale lives in `docs/superpowers/specs/2026-08-13-ecs-deployment-design.md`.

## One-time bootstrap

Terraform cannot store its own state in a bucket it has not created, so the
state bucket and lock table are created outside Terraform. Run once, ever:

```bash
aws s3api create-bucket \
  --bucket inboxpilot-tfstate \
  --region ap-south-1 \
  --create-bucket-configuration LocationConstraint=ap-south-1

aws s3api put-bucket-versioning \
  --bucket inboxpilot-tfstate \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket inboxpilot-tfstate \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-public-access-block \
  --bucket inboxpilot-tfstate \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

aws dynamodb create-table \
  --table-name inboxpilot-tflock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region ap-south-1
```

The bucket is versioned, encrypted and private because **state carries the
generated RDS password**. Treat it as a secret store.

## First deploy

Order matters. Secrets must exist before Terraform plans, and the certificate
must validate before the HTTPS listener can be created.

### 1. Fill in `infra/terraform.tfvars`

```hcl
api_domain      = "api.yourdomain.com"
frontend_origin = "https://your-app.vercel.app"
github_repo     = "owner/InboxPilot"
```

`frontend_origin` must match the Vercel origin **exactly**, scheme included and
no trailing slash, or browser uploads fail the S3 CORS check.

### 1a. This account is shared — what that does and does not mean

Account `061039771642` also runs the `chronon-ai` project. InboxPilot gets its
own VPC (`10.20.0.0/16`), which does not overlap `chronon-ai-prod-vpc`
(`10.1.0.0/16`) or `chronon-ai-staging-vpc` (`10.0.0.0/16`), and every resource
is prefixed `inboxpilot-`. There is no peering and no shared subnet or security
group. Isolation is at the VPC level; IAM, billing and blast radius are shared.

One resource genuinely cannot be duplicated: **the GitHub Actions OIDC
provider**. It is account-scoped, and this account already has one. Hence:

```hcl
create_github_oidc_provider = false   # the default
```

Leave it false here. Terraform references the existing provider via a data
source. Set it to `true` only when applying into a fresh AWS account that has
never had a GitHub OIDC provider — otherwise the apply fails with
`EntityAlreadyExists`. Per-repo scoping is enforced on the deploy role's trust
policy (`token.actions.githubusercontent.com:sub`), not on the shared provider,
so sharing it grants `chronon-ai` nothing.

### 2. Push the secrets

```bash
./scripts/push-secrets.sh .env
```

Fails loudly listing any key that is empty. **`GOOGLE_TOKEN_ENCRYPTION_KEYS`
must be the exact value already in use** — a different key makes every stored
Google OAuth token undecryptable and silently disconnects every user's Gmail and
Calendar. The same is true of `JWT_SECRET` for sessions, less catastrophically.

### 3. Apply, and complete DNS validation

```bash
cd infra
terraform init
terraform apply
```

The apply blocks at `aws_acm_certificate_validation`. In another terminal:

```bash
cd infra && terraform output acm_validation_records
```

Create that CNAME at your DNS provider. The apply continues on its own once the
record resolves, up to a 30-minute timeout.

### 4. Point the domain at the ALB

```bash
cd infra && terraform output alb_dns_name
```

Create a CNAME (or a Route53 alias A record) for your `api_domain` pointing
there. Confirm:

```bash
curl -I https://api.yourdomain.com/health
```

This returns **503 until the first image is deployed** — the ALB exists but has
no healthy target. That is expected at this point.

### 5. Deploy the first image

```bash
cd infra
gh secret set AWS_DEPLOY_ROLE --body "$(terraform output -raw github_deploy_role_arn)"
gh secret set API_DOMAIN --body "api.yourdomain.com"
cd .. && git push origin main
```

The workflow builds ARM64, pushes to ECR, runs migrations, rolls both services,
and finally curls `/health`. It fails rather than reporting green if any step
does not succeed.

### 6. Update the four external consoles

`PUBLIC_BASE_URL` has changed, and nothing warns you about these. Missing one is
silent — mail simply stops being processed, with nothing in the logs.

| Where | What |
|---|---|
| Google Cloud console → OAuth client | Authorised redirect URI → `https://yourdomain.com/api/auth/google/callback` — the **frontend**, not the API. The Next app owns that route and proxies it back; `core.config` defaults to `localhost:3000/api/auth/google/callback` for the same reason. Pointing this at the API gives `redirect_uri_mismatch` at the last step of consent. |
| Google Cloud console → Pub/Sub subscription | Push endpoint → `https://api.yourdomain.com/v1/webhooks/gmail` |
| Recall dashboard → workspace webhook | `https://api.yourdomain.com/v1/webhooks/meeting-bot` |
| Razorpay dashboard → webhooks | `https://api.yourdomain.com/v1/webhooks/razorpay` |

Also point the Vercel frontend's API base URL at `https://api.yourdomain.com`.

Paths verified against `src/api/v1/webhooks.py` (`/gmail`, `/meeting-bot`,
`/razorpay` under the router's `/webhooks` prefix), `src/api/v1/auth.py`
(`/google/callback`), and `API_V1_PREFIX = "/v1"` in `src/core/config.py`.

## Verification

Run all six after the first deploy.

```bash
# 1. HTTPS through the ALB
curl -sS https://api.yourdomain.com/health          # {"status":"ok"}

# 2. Schema is at head
aws ecs execute-command --cluster inboxpilot \
  --task "$(aws ecs list-tasks --cluster inboxpilot \
    --service-name inboxpilot-api --query 'taskArns[0]' --output text)" \
  --container api --interactive --command "alembic current"

# 3. Worker registered its tasks and beat is ticking
aws logs tail /ecs/inboxpilot/worker --since 5m
#    expect: "celery@... ready", the [tasks] list, "Scheduler: Sending due task"

# 4. No Redis connection errors
aws logs tail /ecs/inboxpilot/api --since 5m | grep -i redis
```

5. **Connect a Google account** end to end through the frontend.
6. **Upload a recording** and confirm the presigned PUT succeeds — this is the
   one that catches a wrong `frontend_origin` in the S3 CORS rule.

### The double-beat check

This is the failure the worker's deployment configuration exists to prevent, so
confirm it deliberately. Trigger a second deploy and watch across the
transition:

```bash
aws logs tail /ecs/inboxpilot/worker --follow --since 1m | grep "Sending due task"
```

Each scheduled sweep must appear **exactly once**. Two of the same task within a
second means two beat schedulers ran concurrently — check that
`deployment_minimum_healthy_percent` is still `0` on `inboxpilot-worker`.

## Rollback

Images are tagged by immutable git SHA, so a rollback is a redeploy of known
bytes, not a rebuild:

```bash
# Recent images, newest first
aws ecr describe-images --repository-name inboxpilot \
  --query 'reverse(sort_by(imageDetails,&imagePushedAt))[:10].[imageTags[0],imagePushedAt]' \
  --output table

# Re-run the deploy workflow at the last good commit
gh workflow run deploy.yml --ref <good-sha>
```

**Migrations do not roll back with the image.** If the bad deploy migrated the
schema, downgrade explicitly first with `alembic downgrade -1` via
`execute-command`, and check that the older image tolerates the current schema
before rolling services back.

## Common failures

| Symptom | Cause |
|---|---|
| Task stops immediately, `exec format error` | Image built for amd64. The workflow must pass `platforms: linux/arm64`, matching `runtime_platform` in `ecs.tf`. |
| `ResourceInitializationError: unable to pull secrets` | Execution role missing `ssm:GetParameters` or the `kms:Decrypt` grant, or a parameter named in `local.secret_arns` does not exist. Re-run `push-secrets.sh`. |
| Task pulls forever, then fails | `assign_public_ip` is false. With no NAT gateway the task cannot reach ECR. |
| ALB returns 503 | No healthy target. Check `/ecs/inboxpilot/api` — usually the app crashed on a missing env var. |
| Health checks fail but the app is up | Health check path is `/`, not `/health`. `/` serves a file from `src/web/`, which does not exist in this repo, and returns 500. |
| Browser upload fails with a CORS error | `frontend_origin` does not exactly match the Vercel origin, scheme included. |
| Every user's Google account disconnected | `GOOGLE_TOKEN_ENCRYPTION_KEYS` differs from the value the tokens were encrypted with. Restore the original. |
| Scheduled sweeps fire twice | Two beat schedulers. See the double-beat check above. |
| `terraform plan` wants to revert the running image | Expected only if `ignore_changes = [task_definition]` was removed from the services in `ecs.tf`. |
| `terraform destroy` refuses on the DB | `deletion_protection = true` on `aws_db_instance.main`, deliberately. Disable it in a separate apply first. |
| `EntityAlreadyExists` on the OIDC provider | `create_github_oidc_provider = true` in an account that already has one. Set it back to `false`. |
| `NoSuchEntity` reading the OIDC provider | The reverse: `false` in a fresh account with no provider yet. Set it to `true` for the first apply. |

## Cost

~$77/month: ALB $17, Fargate ~$28, RDS $15, ElastiCache $12, the rest ~$5.
Estimates — confirm against the AWS pricing calculator.

The dial worth turning first is `worker_cpu` / `worker_memory`. It is sized for
`ffmpeg` transcoding of uploads up to `MEDIA_UPLOAD_MAX_BYTES` (1 GB). If media
uploads are rare, dropping to `256` / `1024` saves ~$10/month.

Deliberately deferred, in the order worth adding them: multi-AZ RDS, a second
API task with autoscaling, CloudWatch alarms on ALB 5xx and worker task count.
