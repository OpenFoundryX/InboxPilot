# Replace all three with real values before the first apply.
#
# api_domain      must be a domain you control — ACM validates it over DNS.
# frontend_origin must exactly match the Vercel origin, scheme included and no
#                 trailing slash, or browser uploads fail the S3 CORS check.
# github_repo     scopes the OIDC deploy role; without it any repo could assume it.

api_domain      = "api.inboxoshq.com"
frontend_origin = "https://inboxoshq.com"
github_repo     = "OpenFoundryX/InboxPilot"

# TEMPORARY — direct operator access to Postgres from the internet.
#
# While these two lines are active the production database is reachable from
# any host in db_allowed_cidrs, guarded by a single password. Home and office
# addresses rotate, so the list goes stale and the temptation is to widen it.
#
# To close it again: set the flag to false, empty the list, `terraform apply`.
# For a permanent answer set bastion_enabled = true instead — an SSM tunnel
# gives the same psql session for ~$3/month with nothing exposed.
db_publicly_accessible = true
db_allowed_cidrs       = ["122.172.82.15/32"] # laptop, 2026-08-22
