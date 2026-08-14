variable "region" {
  description = "AWS region for every resource, including the ACM certificate (an ALB requires its certificate in its own region)."
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
  description = "Fargate CPU units for the worker. Sized for ffmpeg transcoding of uploads up to MEDIA_UPLOAD_MAX_BYTES (1 GB); drop to 256 if uploads are rare."
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

variable "db_publicly_accessible" {
  description = "Expose RDS to the internet. When true the DB subnet group also moves to the public subnets, because a publicly-accessible instance in a subnet with no internet gateway route gets a public IP that nothing can reach. Pair with db_allowed_cidrs — this flag alone does not open the security group."
  type        = bool
  default     = false
}

variable "db_allowed_cidrs" {
  description = "CIDRs allowed to reach Postgres from outside the VPC. Only consulted when db_publicly_accessible is true. Never set this to 0.0.0.0/0 — that is the whole internet against one password."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.db_allowed_cidrs, "0.0.0.0/0")
    error_message = "0.0.0.0/0 exposes the database to the entire internet. List specific addresses."
  }
}

variable "bastion_enabled" {
  description = "Stand up the SSM jump host for reaching RDS, Redis and RabbitMQ without exposing them. Off by default; nothing else depends on it."
  type        = bool
  default     = false
}

variable "bastion_instance_type" {
  description = "Bastion size. t4g.nano is ~$3/month and ample for a port-forward."
  type        = string
  default     = "t4g.nano"
}

variable "rabbitmq_cpu" {
  description = "Fargate CPU units for RabbitMQ. 512 = 0.5 vCPU."
  type        = number
  default     = 512
}

variable "rabbitmq_memory" {
  description = "Fargate memory (MiB) for RabbitMQ. Below 1024 is not advisable: RabbitMQ blocks publishers once it passes 40% of available memory, so 512MiB leaves roughly 200MiB of headroom before the queue stops accepting."
  type        = number
  default     = 1024
}

variable "rabbitmq_image" {
  description = "RabbitMQ image. Pinned to a minor version rather than a floating tag so a restart cannot silently change broker versions under a live queue."
  type        = string
  default     = "rabbitmq:3.13-management"
}

variable "google_redirect_uri" {
  description = "Where Google returns the browser after consent. Defaults to <frontend_origin>/api/auth/google/callback — the FRONTEND, not the API, matching the default in core.config. The Next app proxies it back. Must match the authorised redirect URI in the Google Cloud console exactly."
  type        = string
  default     = ""
}

variable "post_login_redirect_url" {
  description = "Where the API sends the browser once login completes. Defaults to <frontend_origin>/onboarding/connect, matching the default in core.config."
  type        = string
  default     = ""
}

variable "allowed_account_id" {
  description = "The AWS account this stack may be applied to. The account is shared with other projects, so a wrong-profile apply is a real risk; this fails the plan instead of creating a parallel stack somewhere unexpected. Set to \"\" to disable the check."
  type        = string
  default     = "061039771642"
}

variable "create_github_oidc_provider" {
  description = "Create the GitHub Actions OIDC provider. It is ACCOUNT-scoped and can exist only once, so leave this false if any other project in the account already created it — Terraform then references the existing provider instead of failing with EntityAlreadyExists."
  type        = bool
  default     = false
}

variable "image_tag" {
  description = "ECR image tag to run. CI passes the git SHA; the default exists only so the first apply can create task definitions before any image is built."
  type        = string
  default     = "bootstrap"
}
