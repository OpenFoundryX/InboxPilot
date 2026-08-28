## Summary

<!-- What user problem does this solve? -->

## Changes

<!-- Keep this concrete and scoped. -->

## Validation

- [ ] `uv run ruff format --check src tests`
- [ ] `uv run ruff check src tests`
- [ ] `uv run pytest`
- [ ] Migration reviewed, or no schema change

## Safety and operations

- [ ] No secrets, mailbox content, personal data, or production identifiers added
- [ ] OAuth scope impact documented, or no scope change
- [ ] Destructive/external actions require appropriate confirmation, or none added
- [ ] Webhook/Celery retries remain idempotent
- [ ] `.env.example`, operator docs, and release notes updated where needed

`mypy` has known pre-1.0 debt. If this change touches reported files, include
the relevant type-check result in the PR description.

## AI behavior

<!-- If prompts, model inputs, tools, or schemas changed, describe the contract,
failure behavior, privacy/cost impact, and tests. Otherwise write “Not applicable.” -->
