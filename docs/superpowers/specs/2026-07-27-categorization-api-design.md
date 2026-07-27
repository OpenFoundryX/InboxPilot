# Categorization API — Design

**Date:** 2026-07-27
**Status:** Approved, ready for implementation planning

Backend API for the Categorization page's two tabs. **General** manages the
built-in categories and re-runs classification. **Advanced** adds custom
categories, deterministic rules, classifier tuning, and per-category actions.

Out of scope: tests (deferred by explicit decision), forwarding as a
per-category action, and any frontend work.

## Why

Categorization is hardcoded and has no API surface at all. The taxonomy lives in
two module constants that must agree but are not checked against each other:

- `services/classify/classifier.py:19` — `LABELS`, six names plus the guidance
  sentence each one contributes to the system prompt.
- `integrations/composio/gmail.py:40` — `INBOXPILOT_LABELS`, the same six names
  plus Gmail colors, alongside four internal `inboxos-*` labels.

Nothing is per-user, nothing is persisted, and no route touches it.

## Constraints discovered in the codebase

1. **There is no Gmail rename-label action.** `gmail.py:28-30` wires only
   `LIST_LABELS`, `CREATE_LABEL`, `DELETE_LABEL`. A category's Gmail label name
   therefore cannot change after creation without re-labelling every affected
   message.
2. **Composio's `user_id` is the app `User.id`.** Confirmed at
   `api/v1/integrations.py:45`. Workers can load per-user config from the UUID
   they already receive.
3. **Workers have a DB path.** `core/database.py:42-74` — `run_async` plus
   `with_worker_session`, a loop-local NullPool session per call.
4. **Batch label mutation already exists.** `gmail_ops._modify` (line 64) takes
   `add` and `remove` label-id lists, so archive / mark-read / star are one
   Composio round trip alongside the category label itself.
5. **The label-id cache self-heals.** `gmail_ops.resolve_label_id` (line 36)
   does not cache misses, so a label created by the API resolves in the worker
   on next use with no cross-process invalidation.

## Data model — `src/models/categorization.py`

### `EmailCategory` (`email_categories`)

`UniqueConstraint(user_id, key)`; `user_id` FK to `users.id`, `ondelete=CASCADE`,
indexed.

| Column | Type | Notes |
|---|---|---|
| `key` | `String(64)` | **Immutable.** Stable slug (`to_do`). Referenced by rules and `fallback_category_key`. |
| `gmail_label` | `String(64)` | **Immutable.** The Gmail label name. Built-ins seed to the *existing* name (`to do`) so already-labelled mail is never orphaned. |
| `display_name` | `String(64)` | Editable, app-side only. Shown in the UI and used in the LLM prompt. |
| `description` | `Text` | Editable. The guidance sentence for the prompt. |
| `color_bg` | `String(7)` | Hex, e.g. `#fb4c2f`. |
| `color_text` | `String(7)` | Hex. |
| `is_builtin` | `Boolean` | Default `False`. Built-ins cannot be deleted. |
| `is_enabled` | `Boolean` | Default `True`. Disabled categories leave the taxonomy and stop being applied. |
| `sort_order` | `Integer` | Display order. |
| `actions` | `JSONB` | `{"archive": bool, "mark_read": bool, "star": bool}`, default all `False`. |

`display_name` is decoupled from `gmail_label` deliberately — this is what makes
renaming safe given constraint 1. Renaming a category changes what the user and
the LLM see; the Gmail label underneath never moves.

**`actions` has three keys, not four.** Archive and skip-inbox are the same
Gmail mutation (remove `INBOX`), so they are collapsed into `archive`.

### `CategorizationRule` (`categorization_rules`)

`user_id` FK, indexed.

| Column | Type | Notes |
|---|---|---|
| `is_enabled` | `Boolean` | Default `True`. |
| `priority` | `Integer` | Lower is evaluated first. Indexed. |
| `match_type` | `String(32)` | `sender_address` \| `sender_domain` \| `subject_keyword` \| `body_keyword` |
| `match_value` | `String(320)` | |
| `action` | `String(16)` | `assign` \| `exclude` |
| `category_key` | `String(64)`, nullable | Required iff `action == "assign"`. |

`category_key` is a validated string, not a foreign key: `key` is unique only
per user, so an FK would need a composite `(user_id, key)` reference that
complicates deletion for no benefit. The service layer validates on write, and
category deletion cleans up dependents (see below).

### `CategorizationSettings` (`categorization_settings`)

One row per user, `user_id` unique — same singleton shape as `MailmanSettings`
and `MeetingSettings`.

| Column | Type | Notes |
|---|---|---|
| `is_enabled` | `Boolean` | Default `True`. Master switch. |
| `fallback_category_key` | `String(64)`, nullable | `None` = leave unlabelled, today's behaviour. |
| `confidence_threshold` | `Float` | Default `0.0` — never overrides. Valid range 0–1. |
| `model` | `String(64)`, nullable | `None` = `settings.OPENAI_MODEL`. |
| `extra_instructions` | `Text`, nullable | Appended to the system prompt. |
| `last_reclassify_at` | `DateTime(tz)`, nullable | |

## Removing the duplicated taxonomy

`BUILTIN_CATEGORIES` in `services/categorization/store.py` becomes the single
source of truth, merging what `classifier.LABELS` and `INBOXPILOT_LABELS` each
hold half of:

| key | gmail_label | display_name | color_bg / color_text | sort |
|---|---|---|---|---|
| `to_do` | `to do` | To do | `#fb4c2f` / `#ffffff` | 0 |
| `to_follow_up` | `to follow up` | To follow up | `#a479e2` / `#ffffff` | 1 |
| `notification` | `notification` | Notification | `#4a86e8` / `#ffffff` | 2 |
| `fyi` | `fyi` | FYI | `#16a766` / `#ffffff` | 3 |
| `marketing` | `marketing` | Marketing | `#fad165` / `#000000` | 4 |
| `noise` | `noise` | Noise | `#999999` / `#ffffff` | 5 |

Descriptions carry over verbatim from `classifier.LABELS`.

`INBOXPILOT_LABELS` keeps only the four internal `inboxos-*` labels and derives
the six category entries from `BUILTIN_CATEGORIES`, so `ensure_labels` keeps
provisioning exactly what it does today. `classifier.LABELS` and `LABEL_NAMES`
are deleted; callers move to the DB-backed taxonomy.

`sync_last_7_days.py:60` imports `LABEL_NAMES` to decide what counts as
already-labelled. It moves to the user's actual `gmail_label` set.

## Classification pipeline

Replaces the body of `services/classify/apply.py`. Per message:

1. **Load** taxonomy, rules, and settings in one DB read, via
   `run_async(with_worker_session(...))`.
2. **Master switch** — `settings.is_enabled == False` → return `None`, do
   nothing.
3. **Rules pass** — enabled rules in `priority` order, first match wins:
   - `exclude` → return `None` immediately. No label, and **no LLM call**.
   - `assign` → take that category, skip the LLM.
4. **LLM pass** — only if no rule matched. Prompt built from *enabled*
   categories' `display_name` + `description`, plus `extra_instructions`.
   Response schema becomes `{"label": str, "confidence": float}`. If confidence
   is below `confidence_threshold`, or the label is unrecognised, fall back to
   `fallback_category_key`; if that is `None`, apply nothing.
5. **Apply** — a single `gmail_ops._modify` call folding the category label and
   its `actions` together: `archive` → remove `INBOX`, `mark_read` → remove
   `UNREAD`, `star` → add `STARRED`. One round trip rather than one per action.
   `UNREAD` and `STARRED` join `INBOX_LABEL` as constants in `gmail_ops`.

### Rule matching semantics

- `sender_address` — case-insensitive exact match against the address parsed out
  of the `From` header.
- `sender_domain` — case-insensitive match on the part after `@`. Accepted with
  or without a leading `@`; normalised on write.
- `subject_keyword` — case-insensitive substring of the subject.
- `body_keyword` — case-insensitive substring of **the snippet, not the full
  body**. `classify_new_email` receives only `sender`, `subject`, and `snippet`
  from the webhook payload and deliberately never re-fetches from Gmail. The
  field is named `body_keyword` for the user's mental model; the API
  documentation must state the snippet limitation plainly.

### Label provisioning

`POST /categories` creates the Gmail label synchronously so a Composio failure
surfaces to the user immediately rather than silently breaking a later
classification.

This keeps the worker's `_ensure_labels_once` lru_cache (`apply.py:18`) covering
built-ins only, so it never needs invalidating when a user edits their taxonomy
— which would otherwise be a real bug, since the cache is keyed on `user_id`
alone and lives for the worker process's lifetime.

## API — `src/api/v1/categorization.py`

Prefix `/categorization`, tag `categorization`, `CurrentUser` + `DbSession`
dependencies exactly as `api/v1/mailman.py` does. Registered in
`api/router.py`.

### General tab

| Method | Path | Behaviour |
|---|---|---|
| `GET` | `/categories` | List in `sort_order`. Seeds the six built-ins on first call (get-or-create, as `get_or_create_settings` does). |
| `PATCH` | `/categories/{key}` | Partial update: `display_name`, `description`, `color_bg`, `color_text`, `is_enabled`, `sort_order`, `actions`. |
| `POST` | `/reclassify` | `202`. Body `{days: int = 7 (1–90), max_results: int \| None (1–2000)}`. Queues the Celery job, stamps `last_reclassify_at`, returns the task id. |

### Advanced tab

| Method | Path | Behaviour |
|---|---|---|
| `POST` | `/categories` | Create a custom category. Derives `key` and `gmail_label` from `display_name`, creates the Gmail label, then commits. |
| `DELETE` | `/categories/{key}` | Custom only. Deletes rules referencing it and clears `fallback_category_key` if it pointed there. |
| `GET` | `/rules` | List in `priority` order. |
| `POST` | `/rules` | Create; appends at the end of the priority order. |
| `PATCH` | `/rules/{id}` | Partial update. |
| `DELETE` | `/rules/{id}` | |
| `PUT` | `/rules/order` | `{rule_ids: [...]}` — rewrites `priority` to match the given order. Must contain exactly the user's rule ids. |
| `GET` / `PUT` | `/settings` | Tuning knobs; `PUT` is partial, matching `SettingsUpdate` in `schemas/mailman.py`. |

On create, `gmail_label` is the `display_name` trimmed and lowercased, and `key`
is that with every run of non-alphanumeric characters replaced by `_`. So
"Client work" yields `gmail_label = "client work"`, `key = "client_work"` —
matching how the built-ins relate to each other.

**Deleting a custom category leaves its Gmail label in place.** Nothing is
stripped from the user's mail, so the operation is non-destructive and
reversible by hand. The cost is an orphan label InboxPilot no longer manages.

### Model allowlist

New setting `CLASSIFIER_MODELS` in `core/config.py`, comma-separated, defaulting
to `"gpt-4o-mini,gpt-4o"`. `settings.OPENAI_MODEL` is always allowed regardless
of the list, so changing the global default cannot invalidate stored per-user
choices.

## Re-classify job

`src/workers/jobs/reclassify.py`, task name `categorization.reclassify`.

`_queue_backfill_classification` in `sync_last_7_days.py:60` already implements
exactly the needed logic — fetch recent mail, skip anything already carrying one
of our labels, enqueue `classify_new_email` per message, capped. It moves to a
shared helper (`services/categorization/backfill.py`) that both callers use,
rather than being forked. `BACKFILL_CLASSIFY_MAX = 200` stays the cap.

Already-labelled mail is skipped, so re-classify is cheap to re-run and never
rewrites a category the user has already seen.

## Error handling

| Status | Cases |
|---|---|
| `404` | Unknown category `key`; unknown rule id; rule or category belonging to another user. |
| `409` | Deleting a built-in category; `POST /reclassify` while `settings.is_enabled` is `False`. |
| `422` | `action == "assign"` with no `category_key`; `category_key` not in the user's taxonomy; `display_name` slugifying to an existing `key`; a `gmail_label` colliding with an existing label or a reserved `inboxos-*` name; `confidence_threshold` outside 0–1; `model` off the allowlist; `PUT /rules/order` whose id set does not exactly match the user's rules. |
| `502` | Composio label creation fails on `POST /categories`. The DB row is rolled back so no category exists without its Gmail label. |

Attempts to change `key` or `gmail_label` are not an error — those fields are
absent from the update schemas, so they are unaddressable.

## Files

New:
- `src/models/categorization.py`
- `src/schemas/categorization.py`
- `src/services/categorization/__init__.py`, `store.py`, `pipeline.py`, `backfill.py`
- `src/api/v1/categorization.py`
- `src/workers/jobs/reclassify.py`
- one Alembic migration (three tables)

Modified:
- `src/services/classify/classifier.py` — taxonomy becomes a parameter; response gains `confidence`; `LABELS`/`LABEL_NAMES` deleted
- `src/services/classify/apply.py` — delegates to the pipeline
- `src/integrations/composio/gmail.py` — `INBOXPILOT_LABELS` derives its category entries from `BUILTIN_CATEGORIES`
- `src/services/mailman/gmail_ops.py` — `UNREAD` / `STARRED` constants
- `src/workers/jobs/sync_last_7_days.py` — uses the shared backfill helper
- `src/api/router.py` — register the router
- `src/core/config.py` — `CLASSIFIER_MODELS`

## Phasing

**Phase 1 — General.** Models, migration, store with seeding, categories
list/patch, re-classify endpoint and job, pipeline reading the DB taxonomy. The
page's General tab is fully usable at this point; Advanced features are simply
absent, and behaviour with untouched defaults is identical to today's.

**Phase 2 — Advanced.** Custom category create/delete, rules CRUD and
reordering, the rules pass in the pipeline, tuning knobs, per-category actions.
