# Replace all three with real values before the first apply.
#
# api_domain      must be a domain you control — ACM validates it over DNS.
# frontend_origin must exactly match the Vercel origin, scheme included and no
#                 trailing slash, or browser uploads fail the S3 CORS check.
# github_repo     scopes the OIDC deploy role; without it any repo could assume it.

api_domain      = "api.example.com"
frontend_origin = "https://app.example.com"
github_repo     = "owner/InboxPilot"
