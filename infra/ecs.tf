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

  # Non-secret configuration, identical for every role. Secrets arrive from SSM
  # through the secrets block below; everything here is safe to read in the
  # console.
  common_env = [
    { name = "ENVIRONMENT", value = "production" },
    { name = "DEBUG", value = "false" },
    { name = "LOG_LEVEL", value = "INFO" },
    { name = "APP_NAME", value = "inboxos" },

    # CELERY_BROKER_URL is absent here on purpose — it carries the RabbitMQ
    # password, so it arrives from SSM through the secrets block instead.
    { name = "REDIS_URL", value = local.redis_url },
    { name = "CELERY_RESULT_BACKEND", value = local.celery_result_backend },

    { name = "PUBLIC_BASE_URL", value = "https://${var.api_domain}" },
    { name = "FRONTEND_BASE_URL", value = var.frontend_origin },
    { name = "GOOGLE_REDIRECT_URI", value = local.google_redirect_uri },
    { name = "POST_LOGIN_REDIRECT_URL", value = local.post_login_redirect_url },

    # Real AWS S3: both endpoints blank. s3.py:45 refuses the half-configured
    # case where only the public one is set, so both are pinned explicitly
    # rather than left to a default.
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
}

# --------------------------------------------------------------------- api

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  # ARM64 across the board: ~20% cheaper for identical work, and the image
  # (python:3.12-slim, uv, apt ffmpeg) is multi-arch already. This MUST match
  # the --platform the deploy workflow builds with, or the task dies
  # immediately with "exec format error".
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
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

    # Required. With no NAT gateway and no public IP, the task cannot reach ECR
    # to pull its own image and start-up fails with a pull timeout.
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
    # CI updates the task definition out of band. Without this, the next
    # unrelated `terraform apply` would revert production to the "bootstrap"
    # image tag.
    ignore_changes = [task_definition, desired_count]
  }
}

# ------------------------------------------------------------------ worker

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
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

  # THE reason this block is not left at the default. A normal rolling deploy
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

# ----------------------------------------------------------------- migrate
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
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
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
