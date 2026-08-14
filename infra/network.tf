# Public subnets hold the ALB and both Fargate services. Tasks get public IPs so
# they can reach ECR, SSM, Google, OpenAI, Recall and Razorpay without a NAT
# gateway, which would cost ~$32/mo — 40% of this stack's budget.
#
# "Public" describes routing, not reachability: inbound is governed entirely by
# the security groups below, and only the ALB accepts traffic from the internet.
#
# Private subnets hold RDS and ElastiCache. Neither needs egress, so the absence
# of a NAT route costs them nothing and guarantees they cannot be reached from
# outside the VPC at all.

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

# The private subnets deliberately have no route table of their own; they fall
# back to the VPC's main route table, which carries only the local route. No
# egress, by construction rather than by policy.

# ------------------------------------------------------------------ security

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
    description = "To the task security group"
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
    description = "All outbound"
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

  # Operator access from named addresses, only while db_publicly_accessible is
  # on. Empty list = no rule, which is the default.
  #
  # This is the whole protection. Everything reaching the instance from outside
  # the VPC is stopped here or not at all, and behind it is one password
  # guarding users' Google OAuth tokens, mail content and billing records.
  # Narrow it the moment it is not needed, and prefer bastion_enabled instead.
  dynamic "ingress" {
    for_each = var.db_publicly_accessible && length(var.db_allowed_cidrs) > 0 ? [1] : []

    content {
      description = "Postgres from named operator addresses"
      from_port   = 5432
      to_port     = 5432
      protocol    = "tcp"
      cidr_blocks = var.db_allowed_cidrs
    }
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
