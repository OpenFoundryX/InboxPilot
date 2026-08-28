# Security policy

InboxPilot processes email, OAuth credentials, calendar data, attachments, and
meeting recordings. Please treat suspected vulnerabilities as sensitive.

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** / private security advisory feature for
this repository. If private reporting is unavailable, contact the repository
owners through GitHub without including exploit details and ask for a private
channel.

Do not open a public issue and do not include real mailbox content, credentials,
tokens, private URLs, personal information, or customer identifiers in a report.

Please include, where safe:

- Affected version or commit.
- Deployment type and relevant optional features.
- Reproduction steps using synthetic data.
- Expected and observed impact.
- Any temporary mitigation you have identified.

We will acknowledge a complete report as soon as practical, normally within
three business days. We will coordinate disclosure after a fix is available.

## Supported versions

InboxPilot is currently pre-1.0. Security fixes are applied to the latest
release and the default branch. Older pre-1.0 releases may not receive patches;
self-hosters should follow release notes and upgrade promptly.

## Operator responsibilities

- Replace every development credential in `.env.example`.
- Use HTTPS for all public callbacks.
- Encrypt OAuth tokens with a generated `GOOGLE_TOKEN_ENCRYPTION_KEYS` value and
  preserve old keys during rotation.
- Verify Gmail Pub/Sub OIDC tokens in production by configuring
  `GOOGLE_PUBSUB_SA_EMAIL` and, when used, `GOOGLE_PUBSUB_AUDIENCE`.
- Configure Recall and billing webhook signing secrets before enabling those
  integrations.
- Restrict database, Redis, RabbitMQ, object-storage, and MinIO network access.
- Back up PostgreSQL and object storage and test restoration.
- Keep dependencies and container images updated.
- Do not enable `DEBUG` or `SQL_ECHO` in production.
- Review Google OAuth scopes and consent-screen requirements before connecting
  users.

## Security boundaries

The source code can be inspected and self-hosted, but open source alone is not a
security certification. A deployment's security also depends on its OAuth app,
infrastructure, model providers, storage, secrets, logging, retention, and
operational controls.

The hosted InboxPilot service and independent self-hosted installations have
different security and compliance boundaries. Do not infer that an assessment
of one installation applies to another.
