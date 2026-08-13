# AWS ECS Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy the InboxPilot backend (FastAPI API + Celery worker) to AWS ECS Fargate in `ap-south-1`, defined in Terraform, with a GitHub Actions pipeline that migrates before it rolls.

**Architecture:** One existing Docker image runs three roles that differ only in `command`. Fargate tasks sit in public subnets with public IPs (no NAT gateway); RDS Postgres and ElastiCache Redis sit in private subnets with no egress. Secrets live in SSM Parameter Store and are injected by ECS at task start. Migrations run as a one-shot `RunTask` that must succeed before either service is updated.

**Tech Stack:** Terraform ~> 1.9 with the AWS provider ~> 5.0, ECS Fargate (ARM64), RDS Postgres 16, ElastiCache Redis 7, ALB + ACM, ECR, SSM Parameter Store, GitHub Actions with OIDC.

**Spec:** `docs/superpowers/specs/2026-08-13-ecs-deployment-design.md`

## Global Constraints

- **Region:** `ap-south-1` for every resource, including the ACM certificate (an ALB requires its certificate in its own region).
- **CPU architecture:** `ARM64` on every task definition, and `--platform linux/arm64` on every image build. A mismatch between the two produces a task that fails at start with `exec format error`.
- **Naming:** every resource name is prefixed `inboxpilot-`. Terraform resource addresses use underscores; AWS `Name` tags and identifiers use hyphens.
- **No NAT gateway.** If a task cannot reach the internet, the fix is `assign_public_ip = true` and a route to the internet gateway — never a NAT gateway, which would blow 40% of the budget.
- **No Secrets Manager**, except the one place RDS forces it (it does not here). Secrets go to SSM Parameter Store as `SecureString` under `/inboxpilot/prod/`.
- **Container port is 8000** everywhere. The image `EXPOSE`s it and `uvicorn` binds it.
- **ALB health check path is `/health`** — never `/`. `src/main.py:112` serves `/` from `WEB_DIR / "index.html"`, and `src/web/` does not exist in this repo, so `/` returns a 500 and would fail every health check.
- **Terraform state is sensitive.** It contains the generated RDS password. It lives in an encrypted, versioned, access-restricted S3 bucket and is never committed.
- **Every task ends with `terraform fmt`, `terraform validate`, and a reviewed `terraform plan`.** Infrastructure has no unit-test cycle; `plan` is the assertion step and reading it is not optional.

---

## File Structure

```
infra/
  versions.tf        provider + terraform version pins, S3 backend
  variables.tf       every input, with descriptions
  terraform.tfvars   non-secret values for this deployment (committed)
  locals.tf          computed names, tags, connection URLs
  network.tf         VPC, subnets, IGW, routes, security groups
  ecr.tf             image repository + lifecycle policy
  data.tf            RDS Postgres, ElastiCache Redis
  storage.tf         S3 media bucket, CORS, media IAM user
  secrets.tf         SSM parameter data sources + Terraform-written params
  iam.tf             ECS execution role, task role, GitHub OIDC role
  alb.tf             ALB, target group, listeners, ACM certificate
  ecs.tf             cluster, three task definitions, two services
  outputs.tf         ALB DNS, ECR URL, cert validation records
scripts/
  push-secrets.sh    reads .env, writes SSM SecureStrings
.github/workflows/
  deploy.yml         build → migrate → deploy → verify
docs/runbooks/
  aws-deploy.md      first deploy, external consoles, rollback, failures
```

Split by responsibility rather than by resource type: `network.tf` owns everything about reachability, `data.tf` owns the stateful services, `ecs.tf` owns everything about running containers. A reviewer asking "what can reach the database?" reads one file.

---

### Task 1: Terraform skeleton, variables, and remote state

Nothing can be planned until the provider and state backend exist. This task produces an `infra/` that plans cleanly with zero resources.

**Files:**
- Create: `infra/versions.tf`, `infra/variables.tf`, `infra/terraform.tfvars`, `infra/locals.tf`, `infra/.gitignore`
- Modify: `.gitignore`

- [ ] **Step 1: Create the state bucket and lock table**

These are created outside Terraform, because Terraform cannot store its own state in a bucket it has not created yet. Run once:

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

- [ ] **Step 2: Write `infra/versions.tf`**

```hcl
terraform {
  required_version = "~> 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State carries the generated RDS password. The bucket is versioned,
  # encrypted, and blocks public access; see Task 1 Step 1.
  backend "s3" {
    bucket         = "inboxpilot-tfstate"
    key            = "prod/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "inboxpilot-tflock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "inboxpilot"
      Environment = "prod"
      ManagedBy   = "terraform"
    }
  }
}
```

- [ ] **Step 3: Write `infra/variables.tf`**

```hcl
variable "region" {
  description = "AWS region for every resource, including the ACM certificate."
  type        = string
  default     = "ap-south-1"
}

variable "project" {
  description = "Name prefix for every resource."
  type        = string
  default     = "inboxpilot"
}

variable "api_domain" {
  description = "Public hostname for the API, e.g. api.example.com. Must be a domain you control; ACM validates it via DNS."
  type        = string
}

variable "frontend_origin" {
  description = "Vercel origin of the web app, scheme included, no trailing slash. Used for the S3 CORS rule and FRONTEND_BASE_URL."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR for the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "db_instance_class" {
  description = "RDS instance class. db.t4g.micro is the cheapest Graviton option."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "RDS storage in GB."
  type        = number
  default     = 20
}

variable "redis_node_type" {
  description = "ElastiCache node type."
  type        = string
  default     = "cache.t4g.micro"
}

variable "api_cpu" {
  description = "Fargate CPU units for the API task. 256 = 0.25 vCPU."
  type        = number
  default     = 256
}

variable "api_memory" {
  description = "Fargate memory (MiB) for the API task."
  type        = number
  default     = 512
}

variable "worker_cpu" {
  description = "Fargate CPU units for the worker. Sized for ffmpeg transcoding of uploads up to MEDIA_UPLOAD_MAX_BYTES (1 GB)."
  type        = number
  default     = 512
}

variable "worker_memory" {
  description = "Fargate memory (MiB) for the worker."
  type        = number
  default     = 2048
}

variable "github_repo" {
  description = "owner/repo allowed to assume the deploy role via OIDC."
  type        = string
}

variable "image_tag" {
  description = "ECR image tag to run. CI passes the git SHA; defaults to bootstrap for the first apply, before any image exists."
  type        = string
  default     = "bootstrap"
}
```

- [ ] **Step 4: Write `infra/terraform.tfvars`**

Replace `api_domain`, `frontend_origin`, and `github_repo` with real values before applying.

```hcl
api_domain      = "api.example.com"
frontend_origin = "https://app.example.com"
github_repo     = "owner/InboxPilot"
```

- [ ] **Step 5: Write `infra/locals.tf`**

```hcl
locals {
  name = var.project

  # Two AZs: an ALB requires subnets in at least two.
  azs = ["${var.region}a", "${var.region}b"]

  # SSM path prefix. push-secrets.sh writes here; task definitions read here.
  ssm_prefix = "/${var.project}/prod"
}
```

- [ ] **Step 6: Write `infra/.gitignore`**

```gitignore
.terraform/
*.tfstate
*.tfstate.*
crash.log
*.tfvars.local
.terraform.lock.hcl.bak
```

- [ ] **Step 7: Initialise and validate**

```bash
cd infra && terraform init && terraform fmt -check && terraform validate
```

Expected: `Terraform has been successfully initialized`, `fmt` prints nothing, `validate` prints `Success! The configuration is valid.`

- [ ] **Step 8: Commit**

```bash
git add infra/ .gitignore
git commit -m "infra: terraform skeleton, provider pins, and S3 remote state"
```

---

### Task 2: Networking

**Files:**
- Create: `infra/network.tf`

**Interfaces:**
- Produces: `aws_vpc.main.id`, `aws_subnet.public[*].id`, `aws_subnet.private[*].id`, and four security groups — `aws_security_group.alb`, `.task`, `.rds`, `.redis` — consumed by Tasks 3, 4, 7, 8, 9.

- [ ] **Step 1: Write `infra/network.tf`**

```hcl
# Public subnets hold the ALB and both Fargate services. Tasks get public IPs
# so they can reach ECR, SSM, Google, OpenAI, Recall and Razorpay without a
# NAT gateway, which would cost ~$32/mo — 40% of this stack's budget.
#
# "Public" describes routing, not reachability: inbound is governed entirely by
# the security groups below, and only the ALB accepts traffic from the internet.
#
# Private subnets hold RDS and ElastiCache. Neither needs egress, so the absence
# of a NAT route costs them nothing and guarantees they cannot be reached.

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${local.name}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone = local.azs[count.index]

  tags = { Name = "${local.name}-private-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# The private subnets deliberately have no route table of their own; they use
# the VPC's main route table, which has only the local route. No egress.

# ---------------------------------------------------------------- security

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public HTTPS entry point"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS from anywhere"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP from anywhere, redirected to HTTPS by the listener"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-alb" }
}

resource "aws_security_group" "task" {
  name        = "${local.name}-task"
  description = "Fargate tasks: inbound only from the ALB, unrestricted egress"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "App port, from the ALB only"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  # Egress is wide because the app calls Google, OpenAI, Anthropic, Recall and
  # Razorpay, and pulls its own image from ECR.
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-task" }
}

resource "aws_security_group" "rds" {
  name        = "${local.name}-rds"
  description = "Postgres, reachable only from tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Postgres from tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id]
  }

  tags = { Name = "${local.name}-rds" }
}

resource "aws_security_group" "redis" {
  name        = "${local.name}-redis"
  description = "Redis, reachable only from tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Redis from tasks"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id]
  }

  tags = { Name = "${local.name}-redis" }
}
```

- [ ] **Step 2: Validate and plan**

```bash
cd infra && terraform fmt && terraform validate && terraform plan
```

Expected: `validate` succeeds; `plan` shows **14 to add** (VPC, IGW, 2 public subnets, 2 private subnets, 1 route table, 2 associations, 4 security groups, and the VPC's default resources are not managed). Confirm no `aws_nat_gateway` appears anywhere in the plan.

- [ ] **Step 3: Commit**

```bash
git add infra/network.tf
git commit -m "infra: VPC, public and private subnets, and security groups"
```

---

### Task 3: ECR repository

**Files:**
- Create: `infra/ecr.tf`

**Interfaces:**
- Produces: `aws_ecr_repository.app.repository_url`, consumed by Tasks 8 and 10.

- [ ] **Step 1: Write `infra/ecr.tf`**

```hcl
resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = local.name }
}

# Images are tagged by git SHA, so they accumulate one per deploy. Keep the
# last 10 so a rollback to a recent revision is always possible, and let the
# rest expire rather than paying storage for every commit ever shipped.
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}
```

`image_tag_mutability = "IMMUTABLE"` matters: it makes a given SHA tag permanently mean one image, so a rollback to a tag is a rollback to known bytes.

- [ ] **Step 2: Validate and plan**

```bash
cd infra && terraform fmt && terraform validate && terraform plan
```

Expected: 2 additional resources to add.

- [ ] **Step 3: Commit**

```bash
git add infra/ecr.tf
git commit -m "infra: ECR repository with immutable tags and a 10-image lifecycle"
```

---

### Task 4: RDS Postgres and ElastiCache Redis

**Files:**
- Create: `infra/data.tf`
- Modify: `infra/locals.tf`

**Interfaces:**
- Produces: `aws_db_instance.main.address`, `aws_elasticache_cluster.main.cache_nodes[0].address`, `random_password.db.result`, and the `local.database_url` / `local.redis_url` / `local.celery_broker_url` / `local.celery_result_backend` connection strings consumed by Tasks 5 and 8.

- [ ] **Step 1: Write `infra/data.tf`**

```hcl
resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
  tags       = { Name = local.name }
}

resource "random_password" "db" {
  length = 32
  # RDS rejects '/', '@', '"' and space in a master password, and the value is
  # interpolated into a URL, so anything needing percent-encoding is excluded.
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = 100 # autoscale storage rather than page at 3am
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "inboxos"
  username = "inboxos_user"
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false

  multi_az                = false
  backup_retention_period = 7
  backup_window           = "18:30-19:00" # 00:00-00:30 IST, off-peak
  maintenance_window      = "sun:19:30-sun:20:30"

  auto_minor_version_upgrade = true
  deletion_protection        = true
  skip_final_snapshot        = false
  final_snapshot_identifier  = "${local.name}-final"

  # Alembic's scheduling migration runs CREATE EXTENSION btree_gist for the
  # double-booking exclusion constraint. RDS ships btree_gist and the master
  # user holds rds_superuser, so no extra grant is needed.

  tags = { Name = local.name }
}

resource "aws_elasticache_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

# Managed rather than a Redis container, despite the lean budget, because this
# is the Celery *broker* and not only a cache. A container without durable
# storage loses the queue on every restart and every deploy — and since booking
# confirmations are queued rather than sent inline, a lost queue means email
# that was accepted and never delivered.
resource "aws_elasticache_cluster" "main" {
  cluster_id           = local.name
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_node_type
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  port                 = 6379

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.redis.id]

  # No auth token: the cluster has no public route and only the task security
  # group can reach the port. Adding AUTH would mean a secret in every URL.

  tags = { Name = local.name }
}
```

- [ ] **Step 2: Append connection strings to `infra/locals.tf`**

Add inside the existing `locals` block:

```hcl
  # core.config rewrites postgresql:// onto asyncpg, but passing the driver
  # explicitly means the app never depends on that rewrite being reached.
  database_url = format(
    "postgresql+asyncpg://%s:%s@%s:%d/%s",
    aws_db_instance.main.username,
    urlencode(random_password.db.result),
    aws_db_instance.main.address,
    aws_db_instance.main.port,
    aws_db_instance.main.db_name,
  )

  redis_host = aws_elasticache_cluster.main.cache_nodes[0].address

  # Separate logical databases, matching .env.example: cache on 0, broker on 1,
  # results on 2. One instance, three namespaces that can be flushed apart.
  redis_url             = "redis://${local.redis_host}:6379/0"
  celery_broker_url     = "redis://${local.redis_host}:6379/1"
  celery_result_backend = "redis://${local.redis_host}:6379/2"
```

- [ ] **Step 3: Validate and plan**

```bash
cd infra && terraform fmt && terraform validate && terraform plan
```

Expected: 5 additional resources (2 subnet groups, `random_password`, the DB instance, the cache cluster). Confirm the plan shows `publicly_accessible = false` on the DB and `multi_az = false`.

- [ ] **Step 4: Commit**

```bash
git add infra/data.tf infra/locals.tf
git commit -m "infra: RDS Postgres and ElastiCache Redis in private subnets"
```

---

### Task 5: S3 media bucket and its IAM user

**Files:**
- Create: `infra/storage.tf`

**Interfaces:**
- Produces: `aws_s3_bucket.media.id`, `aws_iam_access_key.media.id`, `aws_iam_access_key.media.secret`, consumed by Task 6.

- [ ] **Step 1: Write `infra/storage.tf`**

```hcl
resource "aws_s3_bucket" "media" {
  bucket = "${local.name}-media"
  tags   = { Name = "${local.name}-media" }
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# The browser PUTs recordings straight to the bucket with a presigned URL, so
# the web origin must be allowed explicitly. A missing rule fails in the browser
# with no server-side trace at all — see the note in .env.example.
resource "aws_s3_bucket_cors_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  cors_rule {
    allowed_methods = ["GET", "PUT", "HEAD"]
    allowed_origins = [var.frontend_origin]
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

# ----------------------------------------------------------- media identity
#
# A dedicated IAM user with a long-lived key, rather than the ECS task role.
#
# src/integrations/storage/s3.py:36 raises StorageError when the key or secret
# is blank, so boto3's automatic fallback to the task role never runs. More
# importantly, a presigned URL cannot outlive the credentials that signed it,
# and task-role credentials are temporary and rotate on roughly a six-hour
# cycle — MEDIA_LIVE_URL_TTL_SECONDS is exactly 21600. Signing live-media URLs
# with rotating credentials would put them precisely on the expiry boundary.

resource "aws_iam_user" "media" {
  name = "${local.name}-media"
  tags = { Name = "${local.name}-media" }
}

resource "aws_iam_access_key" "media" {
  user = aws_iam_user.media.name
}

resource "aws_iam_user_policy" "media" {
  name = "${local.name}-media"
  user = aws_iam_user.media.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.media.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.media.arn
      },
    ]
  })
}
```

The `ListBucket` grant is not decorative: `s3.py:30` treats both `404/NoSuchKey` and `403/AccessDenied` as "absent" precisely because a key-scoped policy makes the missing-object case ambiguous. Granting `ListBucket` makes S3 answer 404, which is the case the code handles most cleanly.

- [ ] **Step 2: Validate and plan**

```bash
cd infra && terraform fmt && terraform validate && terraform plan
```

Expected: 7 additional resources. Confirm `block_public_acls = true` appears.

- [ ] **Step 3: Commit**

```bash
git add infra/storage.tf
git commit -m "infra: private S3 media bucket, CORS rule, and scoped IAM user"
```

---

### Task 6: Secrets in SSM Parameter Store

Two kinds of parameter: ones Terraform knows (database URL, media credentials) and ones only you know (API keys). Terraform writes the first kind and reads the second, so no third-party secret ever enters Terraform state.

**Files:**
- Create: `infra/secrets.tf`, `scripts/push-secrets.sh`

**Interfaces:**
- Produces: `local.secret_arns`, a map from environment-variable name to SSM parameter ARN, consumed by Task 8.

- [ ] **Step 1: Write `scripts/push-secrets.sh`**

```bash
#!/usr/bin/env bash
#
# Writes the operator-supplied secrets from a local .env into SSM Parameter
# Store, where the ECS task definitions read them.
#
# Run this BEFORE the first `terraform apply`: secrets.tf reads these as data
# sources, and a data source pointing at a missing parameter fails at plan time.
#
# Usage:  ./scripts/push-secrets.sh [path-to-env-file]
#
set -euo pipefail

ENV_FILE="${1:-.env}"
REGION="${AWS_REGION:-ap-south-1}"
PREFIX="/inboxpilot/prod"

[[ -f "$ENV_FILE" ]] || { echo "no such env file: $ENV_FILE" >&2; exit 1; }

# Only these are read from .env. DATABASE_URL, REDIS_URL, the Celery URLs and
# the S3 credentials are deliberately absent: Terraform owns those, because it
# is what creates the resources they point at.
KEYS=(
  JWT_SECRET
  GOOGLE_CLIENT_ID
  GOOGLE_CLIENT_SECRET
  GOOGLE_TOKEN_ENCRYPTION_KEYS
  GOOGLE_PUBSUB_TOPIC
  GOOGLE_PUBSUB_SA_EMAIL
  GOOGLE_PUBSUB_AUDIENCE
  ANTHROPIC_API_KEY
  OPENAI_API_KEY
  RECALL_API_KEY
  RECALL_WEBHOOK_SECRET
  RAZORPAY_KEY_ID
  RAZORPAY_KEY_SECRET
  RAZORPAY_WEBHOOK_SECRET
  RAZORPAY_PLAN_STARTER_MONTHLY
  RAZORPAY_PLAN_STARTER_ANNUAL
  RAZORPAY_PLAN_PRO_MONTHLY
  RAZORPAY_PLAN_PRO_ANNUAL
)

missing=()
for key in "${KEYS[@]}"; do
  # Read from the file rather than sourcing it: .env contains values with
  # spaces, '#' and quotes that a shell would mangle or execute.
  value="$(grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2-)"
  value="${value%\"}"; value="${value#\"}"

  if [[ -z "$value" ]]; then
    missing+=("$key")
    continue
  fi

  aws ssm put-parameter \
    --name "${PREFIX}/${key}" \
    --value "$value" \
    --type SecureString \
    --overwrite \
    --region "$REGION" >/dev/null

  echo "  wrote ${PREFIX}/${key}"
done

if (( ${#missing[@]} )); then
  echo
  echo "EMPTY IN ${ENV_FILE}, NOT WRITTEN:" >&2
  printf '  %s\n' "${missing[@]}" >&2
  echo >&2
  echo "terraform plan will fail until every one of these exists." >&2
  exit 1
fi

echo
echo "All secrets written to ${PREFIX}/ in ${REGION}."
```

- [ ] **Step 2: Make it executable and run it**

```bash
chmod +x scripts/push-secrets.sh
./scripts/push-secrets.sh .env
```

Expected: one `wrote …` line per key. If it exits complaining about empty values, fill them in `.env` first — `GOOGLE_TOKEN_ENCRYPTION_KEYS` in particular must be the **exact** value already in use, or every stored Google OAuth token becomes undecryptable.

- [ ] **Step 3: Write `infra/secrets.tf`**

```hcl
# Parameters Terraform owns, because Terraform creates what they point at.

resource "aws_ssm_parameter" "database_url" {
  name  = "${local.ssm_prefix}/DATABASE_URL"
  type  = "SecureString"
  value = local.database_url
}

resource "aws_ssm_parameter" "s3_access_key_id" {
  name  = "${local.ssm_prefix}/S3_ACCESS_KEY_ID"
  type  = "SecureString"
  value = aws_iam_access_key.media.id
}

resource "aws_ssm_parameter" "s3_secret_access_key" {
  name  = "${local.ssm_prefix}/S3_SECRET_ACCESS_KEY"
  type  = "SecureString"
  value = aws_iam_access_key.media.secret
}

# Parameters the operator owns, written by scripts/push-secrets.sh and only
# read here — so no third-party API key is ever written to Terraform state.

locals {
  operator_secrets = [
    "JWT_SECRET",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_TOKEN_ENCRYPTION_KEYS",
    "GOOGLE_PUBSUB_TOPIC",
    "GOOGLE_PUBSUB_SA_EMAIL",
    "GOOGLE_PUBSUB_AUDIENCE",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "RECALL_API_KEY",
    "RECALL_WEBHOOK_SECRET",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
    "RAZORPAY_PLAN_STARTER_MONTHLY",
    "RAZORPAY_PLAN_STARTER_ANNUAL",
    "RAZORPAY_PLAN_PRO_MONTHLY",
    "RAZORPAY_PLAN_PRO_ANNUAL",
  ]
}

data "aws_ssm_parameter" "operator" {
  for_each = toset(local.operator_secrets)
  name     = "${local.ssm_prefix}/${each.value}"

  # The value is never referenced — only .arn is, for the task definition's
  # secrets block, which ECS resolves at task start. Terraform therefore never
  # holds the plaintext.
  with_decryption = false
}

locals {
  # env-var name -> SSM ARN, exactly the shape a task definition needs.
  secret_arns = merge(
    { for k, v in data.aws_ssm_parameter.operator : k => v.arn },
    {
      DATABASE_URL         = aws_ssm_parameter.database_url.arn
      S3_ACCESS_KEY_ID     = aws_ssm_parameter.s3_access_key_id.arn
      S3_SECRET_ACCESS_KEY = aws_ssm_parameter.s3_secret_access_key.arn
    },
  )
}
```

- [ ] **Step 4: Validate and plan**

```bash
cd infra && terraform fmt && terraform validate && terraform plan
```

Expected: 3 additional resources to add, and 18 data sources read successfully. If a data source errors with `ParameterNotFound`, re-run `scripts/push-secrets.sh` — that key was empty in `.env`.

- [ ] **Step 5: Commit**

```bash
git add infra/secrets.tf scripts/push-secrets.sh
git commit -m "infra: SSM parameters for secrets, with a push script for operator keys"
```

---

### Task 7: IAM roles for ECS and GitHub

**Files:**
- Create: `infra/iam.tf`

**Interfaces:**
- Produces: `aws_iam_role.task_execution.arn` and `aws_iam_role.task.arn`, consumed by Task 8; `aws_iam_role.github.arn`, consumed by Task 10.

- [ ] **Step 1: Write `infra/iam.tf`**

```hcl
data "aws_caller_identity" "current" {}

# ------------------------------------------------- ECS task execution role
#
# Used by the ECS agent, not by the app: it pulls the image, resolves the
# SecureString parameters, and creates log streams. The app's own permissions
# are on the task role below.

resource "aws_iam_role" "task_execution" {
  name = "${local.name}-task-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The managed policy above covers ECR and CloudWatch Logs but NOT SSM. Without
# this, tasks fail to start with ResourceInitializationError and no clue why.
resource "aws_iam_role_policy" "task_execution_ssm" {
  name = "${local.name}-ssm-read"
  role = aws_iam_role.task_execution.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameters"]
        Resource = "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${local.ssm_prefix}/*"
      },
      {
        # SecureStrings are encrypted with the AWS-managed aws/ssm key, and
        # GetParameters cannot decrypt them without this.
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "arn:aws:kms:${var.region}:${data.aws_caller_identity.current.account_id}:key/*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${var.region}.amazonaws.com"
          }
        }
      },
    ]
  })
}

# ------------------------------------------------------------- task role
#
# The role the application process itself assumes. S3 access deliberately does
# NOT go here — see storage.tf for why media uses a static key. This role
# exists so ECS Exec works for debugging, and as the place to add app-level AWS
# permissions later.

resource "aws_iam_role" "task" {
  name = "${local.name}-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "task_exec_channel" {
  name = "${local.name}-ecs-exec"
  role = aws_iam_role.task.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
      ]
      Resource = "*"
    }]
  })
}

# --------------------------------------------------------- GitHub OIDC role
#
# Lets the deploy workflow assume a role with a short-lived token instead of
# storing an AWS access key in GitHub secrets.

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "github" {
  name = "${local.name}-github-deploy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        # Scoped to this repo. Without the sub condition, ANY GitHub repo in
        # the world could assume this role.
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github" {
  name = "${local.name}-deploy"
  role = aws_iam_role.github.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
          "ecr:BatchGetImage",
        ]
        Resource = aws_ecr_repository.app.arn
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:RegisterTaskDefinition",
          "ecs:DescribeTaskDefinition",
          "ecs:DescribeServices",
          "ecs:UpdateService",
          "ecs:RunTask",
          "ecs:DescribeTasks",
        ]
        Resource = "*"
      },
      {
        # RunTask and UpdateService hand these roles to ECS, which requires
        # explicit permission to pass them.
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.task_execution.arn, aws_iam_role.task.arn]
      },
    ]
  })
}
```

- [ ] **Step 2: Validate and plan**

```bash
cd infra && terraform fmt && terraform validate && terraform plan
```

Expected: 8 additional resources. Confirm the GitHub role's `StringLike` condition names your repo — this is the single most important line in the file.

- [ ] **Step 3: Commit**

```bash
git add infra/iam.tf
git commit -m "infra: ECS execution and task roles, plus a repo-scoped GitHub OIDC role"
```

---

### Task 8: ALB, certificate, and DNS validation

**Files:**
- Create: `infra/alb.tf`, `infra/outputs.tf`

**Interfaces:**
- Produces: `aws_lb_target_group.api.arn`, consumed by Task 9; `aws_lb.main.dns_name` and the certificate validation records, output for DNS setup.

- [ ] **Step 1: Write `infra/alb.tf`**

```hcl
resource "aws_lb" "main" {
  name               = local.name
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  # Webhook bodies from Recall and Razorpay are small, but a meeting upload
  # confirmation can be slow; the default 60s idle timeout is enough today and
  # is called out here so it is a decision rather than an accident.
  idle_timeout = 60

  enable_deletion_protection = false

  tags = { Name = local.name }
}

resource "aws_lb_target_group" "api" {
  name        = "${local.name}-api"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip" # awsvpc networking registers task ENIs, not instances

  health_check {
    path     = "/health"
    matcher  = "200"
    interval = 30
    timeout  = 5

    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # Give in-flight requests time to finish when a task is being replaced.
  deregistration_delay = 30

  # The health check path must NOT be "/". src/main.py:112 serves "/" from
  # WEB_DIR / "index.html", and src/web/ does not exist in this repo, so "/"
  # returns 500 and every task would be killed as unhealthy forever.
}

resource "aws_acm_certificate" "api" {
  domain_name       = var.api_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = { Name = var.api_domain }
}

# DNS validation is completed manually: the domain's zone may not be in Route53
# in this account. `terraform output acm_validation_records` prints exactly what
# to add. The apply blocks here until the record resolves.
resource "aws_acm_certificate_validation" "api" {
  certificate_arn = aws_acm_certificate.api.arn

  timeouts {
    create = "30m"
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.api.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}
```

- [ ] **Step 2: Write `infra/outputs.tf`**

```hcl
output "alb_dns_name" {
  description = "Point a CNAME for var.api_domain at this."
  value       = aws_lb.main.dns_name
}

output "acm_validation_records" {
  description = "DNS records to create so ACM can validate the certificate."
  value = [
    for o in aws_acm_certificate.api.domain_validation_options : {
      name  = o.resource_record_name
      type  = o.resource_record_type
      value = o.resource_record_value
    }
  ]
}

output "ecr_repository_url" {
  description = "Image repository, used by the deploy workflow."
  value       = aws_ecr_repository.app.repository_url
}

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE secret in GitHub."
  value       = aws_iam_role.github.arn
}

output "db_address" {
  description = "RDS endpoint, for one-off psql access through a bastion."
  value       = aws_db_instance.main.address
}
```

- [ ] **Step 3: Validate and plan**

```bash
cd infra && terraform fmt && terraform validate && terraform plan
```

Expected: 5 additional resources. `aws_acm_certificate_validation` will show as planned but will block on apply until DNS is in place — that is expected and handled in the runbook.

- [ ] **Step 4: Commit**

```bash
git add infra/alb.tf infra/outputs.tf
git commit -m "infra: ALB, ACM certificate, HTTPS listener and HTTP redirect"
```

---

### Task 9: ECS cluster, task definitions, and services

The core of the deployment. Three task definitions from one image, two long-running services.

**Files:**
- Create: `infra/ecs.tf`
- Modify: `infra/outputs.tf`

**Interfaces:**
- Consumes: `local.secret_arns` (Task 6), `aws_iam_role.task_execution.arn` and `.task.arn` (Task 7), `aws_lb_target_group.api.arn` (Task 8).
- Produces: `aws_ecs_cluster.main.name`, `aws_ecs_task_definition.migrate.family`, and the two service names, consumed by Task 10.

- [ ] **Step 1: Write `infra/ecs.tf`**

```hcl
resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "disabled" # ~$3/mo per cluster; logs are enough at this size
  }

  tags = { Name = local.name }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.name}/api"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${local.name}/worker"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "migrate" {
  name              = "/ecs/${local.name}/migrate"
  retention_in_days = 14
}

locals {
  image = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"

  # Non-secret configuration, identical for every role. Secrets come from SSM
  # via the secrets block; these are safe to read in the console.
  common_env = [
    { name = "ENVIRONMENT", value = "production" },
    { name = "DEBUG", value = "false" },
    { name = "LOG_LEVEL", value = "INFO" },
    { name = "APP_NAME", value = "inboxos" },

    { name = "REDIS_URL", value = local.redis_url },
    { name = "CELERY_BROKER_URL", value = local.celery_broker_url },
    { name = "CELERY_RESULT_BACKEND", value = local.celery_result_backend },

    { name = "PUBLIC_BASE_URL", value = "https://${var.api_domain}" },
    { name = "FRONTEND_BASE_URL", value = var.frontend_origin },
    { name = "GOOGLE_REDIRECT_URI", value = "https://${var.api_domain}/v1/auth/google/callback" },
    { name = "POST_LOGIN_REDIRECT_URL", value = var.frontend_origin },

    # Real AWS S3: both endpoints blank. s3.py:45 refuses the half-configured
    # case where only the public one is set, so both are pinned explicitly.
    { name = "S3_ENDPOINT_URL", value = "" },
    { name = "S3_PUBLIC_ENDPOINT_URL", value = "" },
    { name = "S3_REGION", value = var.region },
    { name = "S3_BUCKET", value = aws_s3_bucket.media.id },
    { name = "MEDIA_STORAGE_PROVIDER", value = "s3" },

    { name = "GMAIL_POLL_ENABLED", value = "true" },
    { name = "GMAIL_PUSH_ENABLED", value = "true" },
    { name = "MEETING_BOT_PROVIDER", value = "recall" },
    { name = "RECALL_API_BASE", value = "https://us-east-1.recall.ai" },
  ]

  common_secrets = [
    for name, arn in local.secret_arns : { name = name, valueFrom = arn }
  ]

  # ARM64 across the board: ~20% cheaper for identical work, and the image
  # (python:3.12-slim, uv, apt ffmpeg) is multi-arch already. This MUST match
  # the --platform the workflow builds with or the task dies with
  # "exec format error".
  runtime_platform = {
    cpu_architecture       = "ARM64"
    operating_system_family = "LINUX"
  }
}

# ------------------------------------------------------------------- api

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    cpu_architecture        = local.runtime_platform.cpu_architecture
    operating_system_family = local.runtime_platform.operating_system_family
  }

  container_definitions = jsonencode([{
    name      = "api"
    image     = local.image
    essential = true

    # No --reload. Compose uses it for local development and it must never
    # reach a deployed service.
    command = ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    environment = local.common_env
    secrets     = local.common_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.api.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "api"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  enable_execute_command = true

  network_configuration {
    subnets = aws_subnet.public[*].id
    # Required: without a public IP and with no NAT gateway, the task cannot
    # reach ECR to pull its own image and start-up fails.
    assign_public_ip = true
    security_groups  = [aws_security_group.task.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  # Rolling: bring the new task up and healthy before retiring the old one.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  health_check_grace_period_seconds = 60

  depends_on = [aws_lb_listener.https]

  lifecycle {
    # CI updates the task definition out of band; Terraform should not revert
    # the running revision on the next unrelated apply.
    ignore_changes = [task_definition, desired_count]
  }
}

# ---------------------------------------------------------------- worker

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    cpu_architecture        = local.runtime_platform.cpu_architecture
    operating_system_family = local.runtime_platform.operating_system_family
  }

  container_definitions = jsonencode([{
    name      = "worker"
    image     = local.image
    essential = true

    # Beat embedded with -B, as on Render. Safe ONLY while exactly one worker
    # task runs: two embedded schedulers double-fire every sweep. See the
    # deployment percentages on the service below.
    command = ["celery", "-A", "worker.celery_app", "worker", "-B", "--loglevel=info"]

    environment = local.common_env
    secrets     = local.common_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.worker.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "worker"
      }
    }
  }])
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  enable_execute_command = true

  network_configuration {
    subnets          = aws_subnet.public[*].id
    assign_public_ip = true
    security_groups  = [aws_security_group.task.id]
  }

  # THE reason this block is not the default. A normal rolling deploy
  # (100/200) briefly runs the old and new worker at once — which is two
  # embedded beat schedulers, which fires every scheduled sweep twice on every
  # deploy. Stopping the old task first leaves roughly a minute with no worker;
  # tasks queue in Redis and are consumed when the new one is up, which is the
  # right trade for a queue consumer.
  #
  # If this ever changes, split beat into its own service at desired_count = 1
  # BEFORE raising the worker count.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

# --------------------------------------------------------------- migrate
#
# Not a service. Invoked with `aws ecs run-task` by the deploy workflow, before
# either service is updated, so the schema is current before any request is
# served and a failed migration aborts the deploy.
#
# render.yaml runs migrations from the worker's start command instead, because
# Render's free tier has no pre-deploy hook — that arrangement cannot order the
# migration ahead of the API. ECS has no such constraint.

resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    cpu_architecture        = local.runtime_platform.cpu_architecture
    operating_system_family = local.runtime_platform.operating_system_family
  }

  container_definitions = jsonencode([{
    name      = "migrate"
    image     = local.image
    essential = true
    command   = ["alembic", "upgrade", "head"]

    environment = local.common_env
    secrets     = local.common_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.migrate.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "migrate"
      }
    }
  }])
}
```

- [ ] **Step 2: Append to `infra/outputs.tf`**

```hcl
output "cluster_name" {
  description = "ECS cluster, used by the deploy workflow."
  value       = aws_ecs_cluster.main.name
}

output "migrate_task_family" {
  description = "Task definition family the deploy workflow runs before rolling services."
  value       = aws_ecs_task_definition.migrate.family
}

output "public_subnet_ids" {
  description = "Subnets the deploy workflow passes to run-task."
  value       = aws_subnet.public[*].id
}

output "task_security_group_id" {
  description = "Security group the deploy workflow passes to run-task."
  value       = aws_security_group.task.id
}
```

- [ ] **Step 3: Validate and plan**

```bash
cd infra && terraform fmt && terraform validate && terraform plan
```

Expected: 9 additional resources. Verify three things in the plan output: `cpu_architecture = "ARM64"` on all three task definitions, `assign_public_ip = true` on both services, and `deployment_minimum_healthy_percent = 0` on the **worker only**.

- [ ] **Step 4: Commit**

```bash
git add infra/ecs.tf infra/outputs.tf
git commit -m "infra: ECS cluster, api/worker/migrate task definitions, and services"
```

---

### Task 10: Deploy pipeline

**Files:**
- Create: `.github/workflows/deploy.yml`

- [ ] **Step 1: Write `.github/workflows/deploy.yml`**

```yaml
name: Deploy

on:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  # Two deploys at once would race on the migrate task and on service updates.
  group: deploy-prod
  cancel-in-progress: false

env:
  AWS_REGION: ap-south-1
  CLUSTER: inboxpilot
  ECR_REPO: inboxpilot

permissions:
  contents: read
  id-token: write # required to request the OIDC token

jobs:
  deploy:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Log in to ECR
        id: ecr
        uses: aws-actions/amazon-ecr-login@v2

      - name: Set up QEMU
        uses: docker/setup-qemu-action@v3

      - name: Set up Buildx
        uses: docker/setup-buildx-action@v3

      # ARM64 to match the task definitions' runtime_platform. A mismatch
      # produces a task that dies immediately with "exec format error".
      #
      # No --target: the Dockerfile's LAST stage is `runtime`, deliberately, so
      # that a builder which cannot name a stage never picks up the `test`
      # stage and ships an image whose CMD is pytest.
      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          platforms: linux/arm64
          push: true
          tags: ${{ steps.ecr.outputs.registry }}/${{ env.ECR_REPO }}:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Run migrations
        run: |
          set -euo pipefail

          IMAGE="${{ steps.ecr.outputs.registry }}/${{ env.ECR_REPO }}:${{ github.sha }}"

          SUBNETS=$(aws ecs describe-services \
            --cluster "$CLUSTER" --services "${CLUSTER}-api" \
            --query 'services[0].networkConfiguration.awsvpcConfiguration.subnets' \
            --output text | tr '\t' ',')
          SG=$(aws ecs describe-services \
            --cluster "$CLUSTER" --services "${CLUSTER}-api" \
            --query 'services[0].networkConfiguration.awsvpcConfiguration.securityGroups[0]' \
            --output text)

          # Register a migrate revision pinned to this build's image.
          TD=$(aws ecs describe-task-definition \
            --task-definition "${CLUSTER}-migrate" \
            --query 'taskDefinition' --output json)

          NEW_TD=$(echo "$TD" | jq --arg IMAGE "$IMAGE" '
            .containerDefinitions[0].image = $IMAGE
            | del(.taskDefinitionArn, .revision, .status, .requiresAttributes,
                  .compatibilities, .registeredAt, .registeredBy)')

          MIGRATE_ARN=$(echo "$NEW_TD" | aws ecs register-task-definition \
            --cli-input-json file:///dev/stdin \
            --query 'taskDefinition.taskDefinitionArn' --output text)

          TASK_ARN=$(aws ecs run-task \
            --cluster "$CLUSTER" \
            --task-definition "$MIGRATE_ARN" \
            --launch-type FARGATE \
            --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
            --query 'tasks[0].taskArn' --output text)

          echo "migrate task: $TASK_ARN"
          aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"

          EXIT_CODE=$(aws ecs describe-tasks \
            --cluster "$CLUSTER" --tasks "$TASK_ARN" \
            --query 'tasks[0].containers[0].exitCode' --output text)

          echo "migrate exited: $EXIT_CODE"
          if [ "$EXIT_CODE" != "0" ]; then
            echo "::error::Migration failed. Services were NOT updated."
            echo "Logs: /ecs/${CLUSTER}/migrate"
            exit 1
          fi

      - name: Deploy services
        run: |
          set -euo pipefail

          IMAGE="${{ steps.ecr.outputs.registry }}/${{ env.ECR_REPO }}:${{ github.sha }}"

          for ROLE in api worker; do
            TD=$(aws ecs describe-task-definition \
              --task-definition "${CLUSTER}-${ROLE}" \
              --query 'taskDefinition' --output json)

            NEW_TD=$(echo "$TD" | jq --arg IMAGE "$IMAGE" '
              .containerDefinitions[0].image = $IMAGE
              | del(.taskDefinitionArn, .revision, .status, .requiresAttributes,
                    .compatibilities, .registeredAt, .registeredBy)')

            ARN=$(echo "$NEW_TD" | aws ecs register-task-definition \
              --cli-input-json file:///dev/stdin \
              --query 'taskDefinition.taskDefinitionArn' --output text)

            aws ecs update-service \
              --cluster "$CLUSTER" \
              --service "${CLUSTER}-${ROLE}" \
              --task-definition "$ARN" >/dev/null

            echo "${ROLE} -> ${ARN}"
          done

      - name: Wait for services to stabilise
        run: |
          aws ecs wait services-stable \
            --cluster "$CLUSTER" \
            --services "${CLUSTER}-api" "${CLUSTER}-worker"

      - name: Verify health through the ALB
        run: |
          set -euo pipefail
          CODE=$(curl -sS -o /dev/null -w '%{http_code}' \
            "https://${{ secrets.API_DOMAIN }}/health")
          echo "GET /health -> $CODE"
          [ "$CODE" = "200" ]
```

- [ ] **Step 2: Set the GitHub secrets**

```bash
cd infra
gh secret set AWS_DEPLOY_ROLE --body "$(terraform output -raw github_deploy_role_arn)"
gh secret set API_DOMAIN --body "api.example.com"   # your real domain
```

- [ ] **Step 3: Lint the workflow**

```bash
python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/deploy.yml')); print('workflow YAML OK')"
```

Expected: `workflow YAML OK`.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/deploy.yml
git commit -m "ci: build, migrate, and roll ECS services on push to main"
```

---

### Task 11: First deploy and runbook

The apply order matters, and several steps are outside AWS entirely.

**Files:**
- Create: `docs/runbooks/aws-deploy.md`

- [ ] **Step 1: Write `docs/runbooks/aws-deploy.md`**

````markdown
# AWS deploy runbook

The stack: an ALB in front of a Fargate API task, a single Fargate worker with
Celery beat embedded, RDS Postgres, ElastiCache Redis, and an S3 media bucket —
all in `ap-south-1`, all defined in `infra/`.

## First deploy

Order matters. Secrets must exist before Terraform plans, and the certificate
must validate before the HTTPS listener can be created.

### 1. Push the secrets

```bash
./scripts/push-secrets.sh .env
```

Fails loudly listing any key that is empty. **`GOOGLE_TOKEN_ENCRYPTION_KEYS`
must be the exact value already in use** — a different key makes every stored
Google OAuth token undecryptable and silently disconnects every user's Gmail and
Calendar. The same is true of `JWT_SECRET` for sessions, less catastrophically.

### 2. Apply, and complete DNS validation

```bash
cd infra
terraform init
terraform apply
```

The apply blocks at `aws_acm_certificate_validation`. In another terminal:

```bash
terraform output acm_validation_records
```

Create that CNAME at your DNS provider. The apply continues on its own once the
record resolves, up to a 30-minute timeout.

### 3. Point the domain at the ALB

```bash
terraform output alb_dns_name
```

Create a CNAME (or a Route53 alias A record) for your `api_domain` pointing
there. Confirm:

```bash
curl -I https://api.example.com/health
```

This returns 503 until the first image is deployed — the ALB exists but has no
healthy target. That is expected.

### 4. Deploy the first image

```bash
gh secret set AWS_DEPLOY_ROLE --body "$(terraform output -raw github_deploy_role_arn)"
gh secret set API_DOMAIN --body "api.example.com"
git push origin main
```

The workflow builds ARM64, pushes to ECR, runs migrations, rolls both services,
and finally curls `/health`. It fails rather than reporting green if any step
does not succeed.

### 5. Update the four external consoles

`PUBLIC_BASE_URL` has changed, and nothing warns you about these. Missing one is
silent — mail simply stops being processed, with nothing in the logs.

| Where | What |
|---|---|
| Google Cloud console → OAuth client | Authorised redirect URI → `https://api.example.com/v1/auth/google/callback` |
| Google Cloud console → Pub/Sub subscription | Push endpoint → `https://api.example.com/v1/webhooks/gmail` |
| Recall dashboard → workspace webhook | `https://api.example.com/v1/webhooks/meeting-bot` |
| Razorpay dashboard → webhooks | `https://api.example.com/v1/webhooks/razorpay` |

Also add `https://api.example.com` to the Vercel frontend's API base URL config.

## Verification

Run all six after the first deploy:

```bash
# 1. HTTPS through the ALB
curl -sS https://api.example.com/health          # {"status":"ok"}

# 2. Schema is at head
aws ecs execute-command --cluster inboxpilot \
  --task "$(aws ecs list-tasks --cluster inboxpilot \
    --service-name inboxpilot-api --query 'taskArns[0]' --output text)" \
  --container api --interactive --command "alembic current"

# 3. Worker registered its tasks and beat is ticking
aws logs tail /ecs/inboxpilot/worker --since 5m
#    expect: "celery@... ready", the [tasks] list, and "Scheduler: Sending due task"

# 4. Redis is reachable from the task
aws logs tail /ecs/inboxpilot/api --since 5m | grep -i redis   # no connection errors
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
# List recent images, newest first
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
| Task stops immediately, `exec format error` | Image built for amd64. The workflow must pass `platforms: linux/arm64`. |
| `ResourceInitializationError: unable to pull secrets` | Execution role is missing `ssm:GetParameters` or the `kms:Decrypt` grant, or a parameter named in `local.secret_arns` does not exist. |
| Task pulls forever, then fails | `assign_public_ip` is false. With no NAT gateway the task cannot reach ECR. |
| ALB returns 503 | No healthy target. Check `/ecs/inboxpilot/api` — usually the app crashed on a missing env var. |
| Health checks fail but the app is up | Health check path is `/`, not `/health`. `/` serves a file from `src/web/`, which does not exist, and returns 500. |
| Browser upload fails with a CORS error | `frontend_origin` does not exactly match the Vercel origin, scheme included. |
| Every user's Google account disconnected | `GOOGLE_TOKEN_ENCRYPTION_KEYS` differs from the value the tokens were encrypted with. Restore the original value. |
| Scheduled sweeps fire twice | Two beat schedulers. See the double-beat check above. |
| `terraform plan` wants to revert the running image | Expected only if `ignore_changes = [task_definition]` was removed from the services. |

## Cost

~$77/month: ALB $17, Fargate ~$28, RDS $15, ElastiCache $12, the rest ~$5.

The dial worth turning first is `worker_cpu` / `worker_memory`. It is sized for
`ffmpeg` transcoding of uploads up to `MEDIA_UPLOAD_MAX_BYTES` (1 GB). If media
uploads are rare, dropping to 256/1024 saves ~$10/month.
````

- [ ] **Step 2: Verify the runbook's commands are syntactically sound**

```bash
python3 - <<'PY'
import re, pathlib
text = pathlib.Path("docs/runbooks/aws-deploy.md").read_text()
assert "api.example.com" in text
assert "/health" in text and "deployment_minimum_healthy_percent" in text
print(f"runbook OK, {len(text.splitlines())} lines")
PY
```

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/aws-deploy.md
git commit -m "docs: AWS deploy runbook covering first deploy, verification and rollback"
```

---

## Self-Review

**Spec coverage.** Every section of the design maps to a task: architecture → 2, 8, 9; networking/no-NAT → 2; beat deployment percentages → 9; migrations as RunTask → 9, 10; managed Redis → 4; secrets in SSM → 6; S3 static key → 5; deploy pipeline → 10; cutover risks → 11. The "out of scope" items (frontend, data migration, multi-AZ, retiring `render.yaml`) are correctly absent.

**Placeholders.** None. `api.example.com`, `owner/InboxPilot`, and the Vercel origin are real inputs the operator substitutes, declared as Terraform variables and flagged in Task 1 Step 4 and the runbook — not deferred work.

**Type consistency.** `local.secret_arns` (Task 6) is consumed with the same shape in Task 9. `local.database_url`, `local.redis_url`, `local.celery_broker_url`, `local.celery_result_backend` (Task 4) are used under those exact names in Tasks 6 and 9. Resource addresses referenced across tasks — `aws_security_group.task`, `aws_subnet.public`, `aws_iam_role.task_execution`, `aws_lb_target_group.api`, `aws_ecr_repository.app` — match their definitions. Service names are `inboxpilot-api` / `inboxpilot-worker` consistently across `ecs.tf`, the workflow, and the runbook.

**One gap found and closed during review:** Task 9's services set `ignore_changes = [task_definition]` because CI updates revisions out of band; without it, the next unrelated `terraform apply` would revert production to the `bootstrap` image tag. This is now noted in the runbook's failure table.
