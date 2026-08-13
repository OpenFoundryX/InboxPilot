#!/usr/bin/env bash
#
# Writes the operator-supplied secrets from a local .env into SSM Parameter
# Store, where the ECS task definitions read them.
#
# Run this BEFORE the first `terraform apply`: infra/secrets.tf reads these as
# data sources, and a data source pointing at a missing parameter fails at plan
# time rather than at apply time.
#
# The KEYS list below must stay in sync with local.operator_secrets in
# infra/secrets.tf. A name there with no parameter here fails the plan; a name
# here and not there is simply never delivered to a container.
#
# Deliberately absent: DATABASE_URL, S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY.
# Terraform owns those, because it is what creates the resources they point at.
#
# Usage:  ./scripts/push-secrets.sh [path-to-env-file]
#
set -euo pipefail

ENV_FILE="${1:-.env}"
REGION="${AWS_REGION:-ap-south-1}"
PREFIX="/inboxpilot/prod"

[[ -f "$ENV_FILE" ]] || { echo "no such env file: $ENV_FILE" >&2; exit 1; }

# Deliberately absent, because no value exists for them yet:
# GOOGLE_PUBSUB_SA_EMAIL, GOOGLE_PUBSUB_AUDIENCE, ANTHROPIC_API_KEY,
# RAZORPAY_WEBHOOK_SECRET. See the comment block in infra/secrets.tf for what
# each one costs. Add a name here and to local.operator_secrets together.
KEYS=(
  JWT_SECRET
  GOOGLE_CLIENT_ID
  GOOGLE_CLIENT_SECRET
  GOOGLE_TOKEN_ENCRYPTION_KEYS
  GOOGLE_PUBSUB_TOPIC
  OPENAI_API_KEY
  RECALL_API_KEY
  RECALL_WEBHOOK_SECRET
  RAZORPAY_KEY_ID
  RAZORPAY_KEY_SECRET
  RAZORPAY_PLAN_STARTER_MONTHLY
  RAZORPAY_PLAN_STARTER_ANNUAL
  RAZORPAY_PLAN_PRO_MONTHLY
  RAZORPAY_PLAN_PRO_ANNUAL
)

echo "Writing to ${PREFIX}/ in ${REGION}, from ${ENV_FILE}"
echo

missing=()
for key in "${KEYS[@]}"; do
  # Read from the file rather than sourcing it: .env holds values with spaces,
  # '#' and quotes that a shell would mangle or execute.
  value="$(grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
  value="${value%\"}"
  value="${value#\"}"

  if [[ -z "$value" ]]; then
    missing+=("$key")
    continue
  fi

  aws ssm put-parameter \
    --name "${PREFIX}/${key}" \
    --value "$value" \
    --type SecureString \
    --overwrite \
    --region "$REGION" >/dev/null

  echo "  wrote ${PREFIX}/${key}"
done

if (( ${#missing[@]} )); then
  echo
  echo "EMPTY IN ${ENV_FILE}, NOT WRITTEN:" >&2
  printf '  %s\n' "${missing[@]}" >&2
  echo >&2
  echo "terraform plan will fail until every one of these exists." >&2
  echo >&2
  echo "GOOGLE_TOKEN_ENCRYPTION_KEYS in particular must be the EXACT value" >&2
  echo "already in use — a different key makes every stored Google OAuth token" >&2
  echo "undecryptable and silently disconnects every user's Gmail and Calendar." >&2
  exit 1
fi

echo
echo "All ${#KEYS[@]} secrets written."
