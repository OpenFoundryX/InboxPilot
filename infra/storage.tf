resource "aws_s3_bucket" "media" {
  bucket = "${local.name}-media"
  tags   = { Name = "${local.name}-media" }
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# The browser PUTs recordings straight to the bucket with a presigned URL, so
# the web origin must be allowed explicitly. A missing rule fails in the browser
# with no server-side trace at all — see the note in .env.example.
resource "aws_s3_bucket_cors_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  cors_rule {
    allowed_methods = ["GET", "PUT", "HEAD"]
    allowed_origins = [var.frontend_origin]
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

# ------------------------------------------------------------ media identity
#
# A dedicated IAM user with a long-lived key, rather than the ECS task role.
#
# src/integrations/storage/s3.py:36 raises StorageError when the key or secret
# is blank, so boto3's automatic fallback to the task role never runs. More
# importantly, a presigned URL cannot outlive the credentials that signed it,
# and task-role credentials are temporary and rotate on roughly a six-hour
# cycle — MEDIA_LIVE_URL_TTL_SECONDS is exactly 21600. Signing live-media URLs
# with rotating credentials would put them precisely on the expiry boundary.

resource "aws_iam_user" "media" {
  name = "${local.name}-media"
  tags = { Name = "${local.name}-media" }
}

resource "aws_iam_access_key" "media" {
  user = aws_iam_user.media.name
}

resource "aws_iam_user_policy" "media" {
  name = "${local.name}-media"
  user = aws_iam_user.media.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.media.arn}/*"
      },
      {
        # Not decorative: s3.py:30 treats both 404/NoSuchKey and
        # 403/AccessDenied as "absent" precisely because a key-scoped policy
        # makes the missing-object case ambiguous. Granting ListBucket makes S3
        # answer 404, which is the case the code handles most cleanly.
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.media.arn
      },
    ]
  })
}
