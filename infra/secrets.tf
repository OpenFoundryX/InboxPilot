# Two kinds of parameter live under local.ssm_prefix.
#
# The ones below are written by Terraform, because Terraform creates the
# resources they point at. Everything else — every third-party API key — is
# written by scripts/push-secrets.sh and only *read* here, so no vendor secret
# is ever committed to Terraform state.

resource "aws_ssm_parameter" "database_url" {
  name  = "${local.ssm_prefix}/DATABASE_URL"
  type  = "SecureString"
  value = local.database_url
}

resource "aws_ssm_parameter" "s3_access_key_id" {
  name  = "${local.ssm_prefix}/S3_ACCESS_KEY_ID"
  type  = "SecureString"
  value = aws_iam_access_key.media.id
}

resource "aws_ssm_parameter" "s3_secret_access_key" {
  name  = "${local.ssm_prefix}/S3_SECRET_ACCESS_KEY"
  type  = "SecureString"
  value = aws_iam_access_key.media.secret
}

locals {
  # Must stay in sync with the KEYS array in scripts/push-secrets.sh. A name
  # here with no parameter in SSM fails at plan time; a name in the script and
  # not here is simply never delivered to a container.
  operator_secrets = [
    "JWT_SECRET",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_TOKEN_ENCRYPTION_KEYS",
    "GOOGLE_PUBSUB_TOPIC",
    "OPENAI_API_KEY",
    "RECALL_API_KEY",
    "RECALL_WEBHOOK_SECRET",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_PLAN_STARTER_MONTHLY",
    "RAZORPAY_PLAN_STARTER_ANNUAL",
    "RAZORPAY_PLAN_PRO_MONTHLY",
    "RAZORPAY_PLAN_PRO_ANNUAL",
  ]

  # Deliberately NOT delivered to containers. Every field in core.config has a
  # default, so an absent parameter means the feature is off — not a crash. Add
  # a name to the list above AND to KEYS in scripts/push-secrets.sh once a value
  # exists; nothing else needs to change.
  #
  #   GOOGLE_PUBSUB_SA_EMAIL   The Gmail push endpoint accepts unauthenticated
  #                            requests without it, which lets anyone who finds
  #                            the URL spend Gmail quota. main.py:51 logs
  #                            "gmail.push_unverified" on every boot as a
  #                            reminder. GMAIL_POLL_ENABLED is on, so mail still
  #                            arrives via the poll either way.
  #
  #   GOOGLE_PUBSUB_AUDIENCE   Optional extra claim check on the push token.
  #
  #   ANTHROPIC_API_KEY        Unused while CLASSIFIER_MODELS is gpt-*.
  #
  #   RAZORPAY_WEBHOOK_SECRET  Currently unused by the app regardless: the
  #                            signature check at api/v1/webhooks.py:174 is
  #                            commented out, so the Razorpay webhook is
  #                            unverified whether or not this is set. Setting
  #                            this parameter does NOT fix that — uncommenting
  #                            that check does.
}

data "aws_ssm_parameter" "operator" {
  for_each = toset(local.operator_secrets)
  name     = "${local.ssm_prefix}/${each.value}"

  # The value is never referenced — only .arn is, for the task definitions'
  # secrets block, which ECS resolves at task start. Terraform therefore never
  # holds the plaintext.
  with_decryption = false
}

locals {
  # env-var name -> SSM ARN, exactly the shape a task definition needs.
  secret_arns = merge(
    { for k, v in data.aws_ssm_parameter.operator : k => v.arn },
    {
      DATABASE_URL         = aws_ssm_parameter.database_url.arn
      S3_ACCESS_KEY_ID     = aws_ssm_parameter.s3_access_key_id.arn
      S3_SECRET_ACCESS_KEY = aws_ssm_parameter.s3_secret_access_key.arn
      CELERY_BROKER_URL    = aws_ssm_parameter.celery_broker_url.arn
    },
  )
}
