# Meeting Notetaker — Design

**Date:** 2026-07-26
**Status:** Approved (approach B)

## Goal

A silent notetaker bot that joins the user's Zoom / Google Meet / Microsoft Teams
calls, records and transcribes them, and turns each call into a recap the rest of
InboxPilot can act on: an email summary, stored transcript, reminders for action
items, and a section in the daily briefing.

## Approach

Recall.ai handles the part that is genuinely hard and undifferentiated — putting a
participant in the room across every platform and getting clean per-speaker audio
out. Everything above that stays in InboxPilot:

| Concern | Owner |
|---|---|
| Joining the meeting, recording, raw transcription | Recall.ai |
| Which meetings qualify for a bot | InboxPilot (existing Composio calendar) |
| Summary, decisions, action-item extraction | InboxPilot (OpenAI, existing pattern) |
| Delivery (email, reminders, digest) | InboxPilot (existing notify/reminders/routines) |

The vendor sits behind a `MeetingBotProvider` protocol, so Skribby or a
self-hosted Attendee can be swapped in without touching anything above the
integration layer.

Deliberately *not* using Recall's own Calendar API: the app already holds a
Google Calendar grant through Composio, and a second OAuth grant for the same
data would mean two sets of per-user join rules to keep consistent.

Cost at time of writing: $0.50/recording-hour + $0.15/hr transcription +
$0.05/hr storage beyond 7 days, no platform fee.

## Components

### `integrations/meetingbot/` — vendor boundary

- `base.py` — `MeetingBotProvider` protocol plus the value types crossing the
  boundary: `BotHandle`, `BotState`, `TranscriptSegment`. Nothing vendor-shaped
  leaks past here.
- `recall.py` — `RecallProvider`. `POST /api/v1/bot` with `join_at` to schedule,
  `DELETE /api/v1/bot/{id}` to cancel, `GET /api/v1/bot/{id}` to read state and
  reach `recordings[].media_shortcuts.transcript.data.download_url`, then a plain
  GET on that URL for the transcript array. Auth is `Authorization: Token <key>`
  against a region host. Webhook verification is Standard Webhooks HMAC-SHA256
  over `{webhook-id}.{webhook-timestamp}.{body}`, base64, against the
  base64-decoded `whsec_` secret; accepts the `svix-*` header aliases.
- `__init__.py` — `get_provider()` resolving `MEETING_BOT_PROVIDER`.

### `services/meetings/`

- `links.py` — pull a meeting URL and platform out of a calendar event
  (`hangoutLink`, `conferenceData.entryPoints`, `location`, `description`) or out
  of arbitrary text, for the paste-a-link path. Pure, no I/O, unit-testable.
- `rules.py` — does this event deserve a bot? Enabled, auto-join on, has a link,
  timed (not all-day), meets the attendee minimum, title not on the skip list,
  starts inside the lookahead window.
- `store.py` — `get_or_create_settings`, plus `upsert_from_event` keyed on
  `(user_id, calendar_event_id)` so a re-run of the sweep never double-books a bot.
- `summarize.py` — transcript → `{summary, decisions, action_items[]}` via OpenAI
  JSON mode, mirroring `services/classify/classifier.py`.
- `recap.py` — render `(subject, body)` for the recap email.
- `digest.py` — the meetings section appended to the daily briefing.

### Data model — `models/meetings.py`

`Meeting` — one row per meeting the notetaker is or was involved with.

- Identity: `user_id`, `source` (`calendar` | `adhoc`), `calendar_event_id`
  (nullable; unique per user when present), `title`, `meeting_url`, `platform`,
  `starts_at`, `ends_at`
- Bot: `bot_id`, `recording_id`, `status`, `status_detail`, `joined_at`
- Output: `transcript`, `summary`, `decisions`, `action_items` (JSONB),
  `recap_sent_at`

Status lifecycle:

```
pending → scheduled → joining → recording → recorded → processed → delivered
                   ↘ cancelled          ↘ failed
```

`MeetingSettings` — one row per user: `enabled`, `auto_join`, `bot_name`,
`min_attendees`, `skip_titles` (JSONB), `lookahead_minutes`, `email_recap`,
`create_reminders`, `include_in_digest`.

### Workers

- `workers/jobs/meetings_sweep.py` (`meetings.sweep`, beat every 60s, under
  `single_run`) — for each user with meetings enabled, read the calendar window
  ahead, apply `rules`, upsert `Meeting` rows, and schedule bots with `join_at`.
  Cancels the bot for any scheduled meeting whose calendar event has vanished.
- `workers/jobs/process_meeting.py` (`meetings.process`) — triggered by
  `bot.done`. Fetches the transcript, persists it, summarizes, writes reminders
  for action items with a due date, sends the recap, marks `delivered`.

### API — `api/v1/meetings.py`

- `GET /v1/meetings` — list, newest first
- `GET /v1/meetings/{id}` — detail including transcript
- `POST /v1/meetings/join` — paste a link, bot joins now (ad-hoc path)
- `DELETE /v1/meetings/{id}/bot` — cancel a scheduled bot
- `GET`/`PUT /v1/meetings/settings`

### Webhook — `POST /v1/webhooks/recall`

Verify, map `bot.*` status codes onto `Meeting.status`, and on `bot.done` enqueue
`meetings.process`. Returns 2xx fast and does no work inline — Recall times out
at 15 seconds. Unknown bot ids are acknowledged and ignored.

## Data flow

```
calendar sweep ─┐
                ├→ Meeting(scheduled) → Recall bot ─→ webhook bot.* → status
paste a link  ─┘                                       └ bot.done → meetings.process
                                                                      ├→ transcript
                                                                      ├→ summary
                                                                      ├→ reminders
                                                                      ├→ recap email
                                                                      └→ digest section
```

## Error handling

- Recall unreachable when scheduling: the meeting stays `pending`; the next sweep
  retries. No bot, no crash.
- `bot.fatal` / recording denied: `failed` with `status_detail` from `sub_code`.
  No recap is sent for a meeting that was never recorded.
- Transcript empty (nobody spoke, bot sat in a waiting room): mark `processed`,
  skip the recap rather than emailing an empty summary.
- Summarization failure: transcript is already persisted, so `meetings.process`
  is safely re-runnable.
- Webhook signature failure: 401, nothing enqueued.
- Idempotency: the sweep is keyed on `(user_id, calendar_event_id)`; the recap
  send is guarded by `recap_sent_at`.

## Consent

Recording other people has legal weight. The bot appears as a named participant
("InboxPilot Notetaker") — visible to everyone, which is the baseline every
platform requires. `auto_join` defaults **off**; a user turns it on deliberately.
Per-meeting opt-in was considered and deferred: the user picked calendar
auto-join and paste-a-link, and the settings row is where the blast radius is
controlled.

## Testing

- `links.py` — extraction across Meet/Zoom/Teams event shapes and messy
  `location`/`description` text; no false positives on unrelated URLs.
- `rules.py` — each skip reason in isolation.
- `recall.py` — webhook signature verify (valid, tampered body, wrong secret,
  `svix-*` aliases); transcript array → `TranscriptSegment` mapping.
- `summarize.py` — mocked OpenAI response → parsed shape, and malformed JSON
  handled without raising.
- Sweep idempotency — running twice over the same calendar window creates one
  Meeting and one bot.

## Out of scope

Live in-meeting participation, speaking, real-time streaming transcription,
video storage/playback, attendee-facing recap emails, and self-hosted bot
infrastructure.
