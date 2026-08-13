resource "aws_lb" "main" {
  name               = local.name
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  # Webhook bodies from Recall and Razorpay are small, but a meeting upload
  # confirmation can be slow. 60s is enough today; stated explicitly so it is a
  # decision rather than an accident.
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
    # MUST NOT be "/". src/main.py:112 serves "/" from WEB_DIR / "index.html",
    # and src/web/ does not exist in this repo, so "/" returns 500 and every
    # task would be killed as unhealthy, forever, with the app working fine.
    path     = "/health"
    matcher  = "200"
    interval = 30
    timeout  = 5

    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # Give in-flight requests time to finish when a task is being replaced.
  deregistration_delay = 30
}

resource "aws_acm_certificate" "api" {
  domain_name       = var.api_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = { Name = var.api_domain }
}

# DNS validation is completed by hand: the domain's zone may not live in Route53
# in this account. `terraform output acm_validation_records` prints exactly what
# to add, and the apply blocks here until the record resolves.
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
