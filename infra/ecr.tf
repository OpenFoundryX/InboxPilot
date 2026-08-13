resource "aws_ecr_repository" "app" {
  name = local.name

  # A given SHA tag permanently means one image, so a rollback to a tag is a
  # rollback to known bytes rather than a rebuild.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = local.name }
}

# Images are tagged by git SHA, so they accumulate one per deploy. Keep the last
# 10 so a rollback to a recent revision is always possible, and let the rest
# expire rather than paying storage for every commit ever shipped.
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}
