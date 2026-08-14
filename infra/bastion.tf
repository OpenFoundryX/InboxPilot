# A jump host for reaching RDS, ElastiCache and RabbitMQ from a laptop.
#
# All three sit in subnets with no route off the VPC, which is deliberate: a
# database reachable from the internet is one password away from being someone
# else's. This box is the sanctioned way in.
#
# It has NO inbound rules at all — not even SSH, and no key pair. Session
# Manager reaches it outbound-only through the SSM agent, so there is nothing
# listening to attack, and access is governed by IAM rather than by who holds a
# private key. Every session is logged in CloudTrail.
#
# ~$3-4/month. Set bastion_enabled = false to remove it when it is not needed;
# nothing else depends on it.

resource "aws_security_group" "bastion" {
  count = var.bastion_enabled ? 1 : 0

  name        = "${local.name}-bastion"
  description = "Jump host: no inbound, egress only"
  vpc_id      = aws_vpc.main.id

  # No ingress block whatsoever. Session Manager works over an outbound
  # connection the SSM agent makes to the service.

  egress {
    description = "SSM endpoints, package updates, and the private data stores"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-bastion" }
}

# The data stores accept traffic only from a named security group, so the
# bastion needs its own rule in each. Separate rules rather than edits to the
# groups themselves, so removing the bastion removes its access with it.

resource "aws_security_group_rule" "rds_from_bastion" {
  count = var.bastion_enabled ? 1 : 0

  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.rds.id
  source_security_group_id = aws_security_group.bastion[0].id
  description              = "Postgres from the bastion"
}

resource "aws_security_group_rule" "redis_from_bastion" {
  count = var.bastion_enabled ? 1 : 0

  type                     = "ingress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  security_group_id        = aws_security_group.redis.id
  source_security_group_id = aws_security_group.bastion[0].id
  description              = "Redis from the bastion"
}

resource "aws_security_group_rule" "rabbitmq_from_bastion" {
  count = var.bastion_enabled ? 1 : 0

  type                     = "ingress"
  from_port                = 15672
  to_port                  = 15672
  protocol                 = "tcp"
  security_group_id        = aws_security_group.rabbitmq.id
  source_security_group_id = aws_security_group.bastion[0].id
  description              = "RabbitMQ management UI from the bastion"
}

# ---------------------------------------------------------------------- IAM

resource "aws_iam_role" "bastion" {
  count = var.bastion_enabled ? 1 : 0

  name = "${local.name}-bastion"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# The only permission it gets. Enough for the SSM agent to register and hold a
# session; nothing else. The bastion cannot read secrets or touch other AWS
# resources — it is a network position, not an identity.
resource "aws_iam_role_policy_attachment" "bastion_ssm" {
  count = var.bastion_enabled ? 1 : 0

  role       = aws_iam_role.bastion[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "bastion" {
  count = var.bastion_enabled ? 1 : 0

  name = "${local.name}-bastion"
  role = aws_iam_role.bastion[0].name
}

# ----------------------------------------------------------------- instance

# Amazon Linux 2023, arm64 — the SSM agent is preinstalled, so there is no user
# data to get wrong.
data "aws_ssm_parameter" "al2023_arm64" {
  count = var.bastion_enabled ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_instance" "bastion" {
  count = var.bastion_enabled ? 1 : 0

  ami           = data.aws_ssm_parameter.al2023_arm64[0].value
  instance_type = var.bastion_instance_type

  subnet_id              = aws_subnet.public[0].id
  vpc_security_group_ids = [aws_security_group.bastion[0].id]
  iam_instance_profile   = aws_iam_instance_profile.bastion[0].name

  # Needed to reach the SSM service. There is no NAT gateway, and without a
  # public IP the agent cannot register — the instance would boot fine and
  # simply never appear as a Session Manager target.
  associate_public_ip_address = true

  # No key_name on purpose. A key pair would be a second way in that IAM does
  # not govern and CloudTrail does not record.

  root_block_device {
    volume_size = 8
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens   = "required" # IMDSv2 only
    http_endpoint = "enabled"
  }

  user_data = <<-EOT
    #!/bin/bash
    # postgresql15 is what AL2023 packages; the client speaks to a 16 server.
    dnf install -y postgresql15 || true
  EOT

  tags = { Name = "${local.name}-bastion" }
}
