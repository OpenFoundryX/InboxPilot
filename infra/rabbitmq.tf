# RabbitMQ as the Celery broker, self-hosted on Fargate.
#
# Redis stays, but only for what it is good at here: cache, locks, rate limits,
# and the Celery result backend. The queue itself moves to AMQP, which is what
# docker-compose has always used locally — so dev and prod speak the same broker
# again.
#
# Single node, deliberately. RabbitMQ clustering on Fargate needs peer discovery
# and stable node names, and a cluster of one is what this workload's throughput
# calls for. That makes it a single point of failure and means a deploy has a
# short broker outage; Celery's broker_connection_retry_on_startup covers the
# reconnect, and durable queues on EFS cover the messages.

resource "aws_service_discovery_private_dns_namespace" "main" {
  name        = "${local.name}.local"
  description = "Service discovery for internal ECS services"
  vpc         = aws_vpc.main.id
}

resource "aws_service_discovery_service" "rabbitmq" {
  name = "rabbitmq"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.main.id

    dns_records {
      ttl  = 10 # short: the address changes on every task replacement
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}

# ------------------------------------------------------------------ storage
#
# Without durable storage a broker restart drops every queued message, which
# would leave us exactly where Redis-as-broker was — and the whole point of
# moving to AMQP is not losing work. EFS survives task replacement and deploys.

resource "aws_security_group" "efs" {
  name        = "${local.name}-efs"
  description = "NFS, reachable only from the RabbitMQ task"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "NFS from RabbitMQ"
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.rabbitmq.id]
  }

  tags = { Name = "${local.name}-efs" }
}

resource "aws_efs_file_system" "rabbitmq" {
  creation_token = "${local.name}-rabbitmq"
  encrypted      = true

  # The mnesia directory is a few MB. Bursting is the right throughput mode and
  # costs nothing extra.
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  tags = { Name = "${local.name}-rabbitmq" }
}

# Mount targets must exist in the subnets the task runs in, which are the public
# ones — the task needs egress to pull its image and there is no NAT gateway.
resource "aws_efs_mount_target" "rabbitmq" {
  count           = 2
  file_system_id  = aws_efs_file_system.rabbitmq.id
  subnet_id       = aws_subnet.public[count.index].id
  security_groups = [aws_security_group.efs.id]
}

# The official image runs as uid/gid 999. The access point creates and owns the
# directory as that user, so the container can write without running as root.
resource "aws_efs_access_point" "rabbitmq" {
  file_system_id = aws_efs_file_system.rabbitmq.id

  posix_user {
    uid = 999
    gid = 999
  }

  root_directory {
    path = "/rabbitmq"

    creation_info {
      owner_uid   = 999
      owner_gid   = 999
      permissions = "755"
    }
  }

  tags = { Name = "${local.name}-rabbitmq" }
}

# ----------------------------------------------------------------- network

resource "aws_security_group" "rabbitmq" {
  name        = "${local.name}-rabbitmq"
  description = "AMQP, reachable only from application tasks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "AMQP from application tasks"
    from_port       = 5672
    to_port         = 5672
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id]
  }

  # The management UI on 15672 is deliberately NOT exposed. Reach it with
  # `aws ecs execute-command` and a port-forward if you need it; publishing it
  # would mean another listener and another password on the internet.

  egress {
    description = "Image pull and EFS"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-rabbitmq" }
}

# --------------------------------------------------------------- credentials

resource "random_password" "rabbitmq" {
  length = 32
  # Interpolated into an amqp:// URL, so restrict to characters that need no
  # percent-encoding. A '%' here would also break alembic's configparser, which
  # is what the DATABASE_URL escape in alembic/env.py exists for.
  special          = true
  override_special = "-_"
}

resource "aws_ssm_parameter" "celery_broker_url" {
  name = "${local.ssm_prefix}/CELERY_BROKER_URL"
  type = "SecureString"

  # The trailing slash is the default vhost, "/" — amqp://host:5672 alone means
  # no vhost and RabbitMQ refuses the connection.
  value = "amqp://${local.rabbitmq_user}:${random_password.rabbitmq.result}@${local.rabbitmq_host}:5672/%2F"
}

resource "aws_ssm_parameter" "rabbitmq_password" {
  name  = "${local.ssm_prefix}/RABBITMQ_DEFAULT_PASS"
  type  = "SecureString"
  value = random_password.rabbitmq.result
}

# The EFS volume is mounted with IAM authorization, so the *task* role — not
# the execution role — needs mount rights, scoped to this one access point.
resource "aws_iam_role_policy" "task_efs" {
  name = "${local.name}-efs"
  role = aws_iam_role.task.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "elasticfilesystem:ClientMount",
        "elasticfilesystem:ClientWrite",
      ]
      Resource = aws_efs_file_system.rabbitmq.arn
      Condition = {
        StringEquals = {
          "elasticfilesystem:AccessPointArn" = aws_efs_access_point.rabbitmq.arn
        }
      }
    }]
  })
}

# ------------------------------------------------------------------ service

resource "aws_cloudwatch_log_group" "rabbitmq" {
  name              = "/ecs/${local.name}/rabbitmq"
  retention_in_days = 14
}

resource "aws_ecs_task_definition" "rabbitmq" {
  family                   = "${local.name}-rabbitmq"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.rabbitmq_cpu
  memory                   = var.rabbitmq_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  volume {
    name = "rabbitmq-data"

    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.rabbitmq.id
      transit_encryption = "ENABLED"

      authorization_config {
        access_point_id = aws_efs_access_point.rabbitmq.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name      = "rabbitmq"
    image     = var.rabbitmq_image
    essential = true

    portMappings = [{ containerPort = 5672, protocol = "tcp" }]

    environment = [
      { name = "RABBITMQ_DEFAULT_USER", value = local.rabbitmq_user },
      { name = "RABBITMQ_DEFAULT_VHOST", value = "/" },
      # Fixed so the mnesia directory on EFS keeps the same name across task
      # replacements. Without it RabbitMQ derives the node name from the
      # container hostname, which changes every deploy, and it comes up as a
      # brand new empty node beside the old data.
      { name = "RABBITMQ_NODENAME", value = "rabbit@inboxpilot" },
    ]

    secrets = [
      { name = "RABBITMQ_DEFAULT_PASS", valueFrom = aws_ssm_parameter.rabbitmq_password.arn },
    ]

    mountPoints = [{
      sourceVolume  = "rabbitmq-data"
      containerPath = "/var/lib/rabbitmq"
      readOnly      = false
    }]

    healthCheck = {
      command     = ["CMD", "rabbitmq-diagnostics", "-q", "ping"]
      interval    = 30
      timeout     = 10
      retries     = 5
      startPeriod = 60
    }

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.rabbitmq.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "rabbitmq"
      }
    }
  }])
}

resource "aws_ecs_service" "rabbitmq" {
  name            = "${local.name}-rabbitmq"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.rabbitmq.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  enable_execute_command = true

  network_configuration {
    subnets          = aws_subnet.public[*].id
    assign_public_ip = true
    security_groups  = [aws_security_group.rabbitmq.id]
  }

  service_registries {
    registry_arn = aws_service_discovery_service.rabbitmq.arn
  }

  # Two tasks must never run at once: both would mount the same EFS directory
  # and the second would find mnesia already locked, or worse, corrupt it. Stop
  # the old one first — the same reasoning as the worker, for different stakes.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  health_check_grace_period_seconds = 0

  depends_on = [aws_efs_mount_target.rabbitmq]

  lifecycle {
    ignore_changes = [desired_count]
  }
}
