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
