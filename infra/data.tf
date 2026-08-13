resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
  tags       = { Name = local.name }
}

resource "random_password" "db" {
  length = 32

  # RDS rejects '/', '@', '"' and space in a master password, and the value is
  # interpolated into a connection URL, so anything needing percent-encoding is
  # excluded here rather than escaped later.
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
  backup_window           = "18:30-19:00"         # 00:00-00:30 IST, off-peak
  maintenance_window      = "sun:19:30-sun:20:30" # 01:00 IST Monday

  auto_minor_version_upgrade = true
  deletion_protection        = true
  skip_final_snapshot        = false
  final_snapshot_identifier  = "${local.name}-final"

  # The scheduling migration runs CREATE EXTENSION btree_gist for the
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

  # No auth token: the cluster sits in a subnet with no internet route and only
  # the task security group can reach the port. Adding AUTH would put a secret
  # in three connection URLs for no additional isolation.

  tags = { Name = local.name }
}
