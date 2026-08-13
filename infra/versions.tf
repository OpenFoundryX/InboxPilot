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

  # State carries the generated RDS password, so the bucket is versioned,
  # encrypted, and blocks public access. See docs/runbooks/aws-deploy.md for the
  # one-time commands that create it — Terraform cannot store its own state in a
  # bucket it has not created yet.
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
