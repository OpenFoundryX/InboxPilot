locals {
  name = var.project

  # Two AZs: an ALB requires subnets in at least two.
  azs = ["${var.region}a", "${var.region}b"]

  # SSM path prefix. scripts/push-secrets.sh writes here; the task definitions
  # read here. The two must agree or tasks fail to start with
  # ResourceInitializationError.
  ssm_prefix = "/${var.project}/prod"

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

  # Both point at the FRONTEND, not the API — the Next app owns
  # /api/auth/google/callback and proxies it back. core.config defaults to
  # localhost:3000 for exactly this reason. Deriving these from api_domain
  # instead sends Google to a path the API does not serve, and consent fails at
  # the last step with redirect_uri_mismatch.
  google_redirect_uri     = var.google_redirect_uri != "" ? var.google_redirect_uri : "${var.frontend_origin}/api/auth/google/callback"
  post_login_redirect_url = var.post_login_redirect_url != "" ? var.post_login_redirect_url : "${var.frontend_origin}/onboarding/connect"
  login_url               = "${var.frontend_origin}/login"

  redis_host = aws_elasticache_cluster.main.cache_nodes[0].address

  # Redis is cache, locks and rate limits on db 0, and the Celery *result*
  # backend on db 2. The broker is RabbitMQ — db 1 is deliberately unused now,
  # left free rather than renumbered so existing keys keep their meaning.
  redis_url             = "redis://${local.redis_host}:6379/0"
  celery_result_backend = "redis://${local.redis_host}:6379/2"

  # Resolved through Cloud Map inside the VPC. The task's IP changes on every
  # replacement, which is why nothing may cache this address.
  rabbitmq_user = "inboxos"
  rabbitmq_host = "rabbitmq.${aws_service_discovery_private_dns_namespace.main.name}"
}
