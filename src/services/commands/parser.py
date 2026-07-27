"""Parse a self-email into a typed list of InboxPilot commands (via the LLM).

Any email a user sends to themselves is treated as a candidate command. The LLM
extracts zero or more structured actions; an empty list means "not a command"
(e.g. a plain personal note), which the caller files silently.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI

from core.config import settings
from core.logging import get_logger
from models.categorization import BUILTIN_CATEGORIES

log = get_logger(__name__)

_GMAIL_LABELS = [c.gmail_label for c in BUILTIN_CATEGORIES]

# Confirmation replies we send carry this subject prefix; never treat them as
# commands (loop guard, belt-and-suspenders alongside the inboxos-rules label).
REPLY_SUBJECT_PREFIX = "✓ InboxOS:"

_SYSTEM = f"""You convert a user's email-to-self into structured commands for their
email assistant. Return ONLY JSON: {{"actions": [...], "summary": "<short human summary>"}}.

If the email contains no actionable instruction (it's just a personal note),
return {{"actions": [], "summary": ""}}.

Each action is one of these shapes (include only the fields that apply):
- {{"type": "create_label", "name": "Receipts"}}
- {{"type": "delete_label", "name": "OldLabel"}}
- {{"type": "set_routine", "delivery_mode": "interval|times|custom_daily",
     "interval_minutes": int, "times_per_day": int, "custom_times": ["09:00","18:00"],
     "active_window_start": "09:00", "active_window_end": "21:00",
     "dnd_enabled": bool, "dnd_start": "22:00", "dnd_end": "07:00", "timezone": "Asia/Kolkata"}}
- {{"type": "add_vip", "domains": ["stripe.com"], "addresses": ["a@b.com"], "keywords": ["OTP"]}}
- {{"type": "remove_vip", "domains": [...], "addresses": [...], "keywords": [...]}}
- {{"type": "create_rule", "criteria": {{"from": "x@y.com"}}, "archive": true, "apply_to_existing": true}}
     (criteria may use from/to/subject/query; also optional: apply_label, archive, star, mark_read, trash;
      set apply_to_existing=true to ALSO apply the effect to mail already in the mailbox, not just future mail)
- {{"type": "manage_routine", "routine": "briefing", "enabled": true, "run_time": "08:00", "weekday": null}}
     (routine is one of: "briefing" (daily digest), "chase_threads" (nudge threads awaiting a reply),
      "reconnect" (people to reach out to), "deadline_scan" (turn deadlines into reminders),
      "catchup" (important unread), "invoices" (invoice summary),
      "double_bookings" (calendar clash heads-up), "schedule_trusted" (draft meeting times for VIP requests);
      weekday 0=Mon..6=Sun for weekly, null=daily; set enabled=false to turn off)
- {{"type": "send_briefing_now"}}  (send a briefing/summary email immediately)
- {{"type": "catch_up_now"}}  (summarize important unread mail — "catch me up", "what did I miss")
- {{"type": "summarize_invoices_now"}}  (summarize recent invoices/receipts/bills)
- {{"type": "scan_deadlines_now"}}  (find deadlines in recent mail and set reminders)
- {{"type": "set_reminder", "remind_at": "2026-07-23T15:00:00+05:30", "title": "Call the bank", "note": "..."}}
     (remind_at is an ISO 8601 datetime WITH the user's timezone offset; resolve relative times
      like "tomorrow 3pm" / "in 2 hours" against the current time given below)

Rules:
- Include ONLY the fields that apply. OMIT every unused field entirely.
- NEVER output placeholder values like "..." — if you don't have a value, leave the field out.
- Only set apply_label when the user explicitly asks to label; do not invent one.
- "deliver N times a day" -> set_routine delivery_mode=times, times_per_day=N.
- "deliver at 1pm and 6pm" -> delivery_mode=custom_daily, custom_times=["13:00","18:00"].
- "every N minutes/hours" -> delivery_mode=interval with interval_minutes.
- If asked to auto-label, apply_label should be one of: {", ".join(_GMAIL_LABELS)} (or a label the user names).
- "archive"/"skip inbox" -> create_rule with archive=true. "delete"/"trash" -> trash=true.
- If the user says "current"/"existing"/"already"/"all my ... (that are already here)" alongside a rule,
  ALSO set apply_to_existing=true so mail already in the mailbox is acted on, not just future mail.
  "archive current AND future X" is a SINGLE create_rule with archive=true and apply_to_existing=true.
- The user's own labels are: {", ".join(_GMAIL_LABELS)}. These are labels, NOT Gmail categories.
  When the user names one of them (e.g. "marketing emails", "my fyi mail"), the criteria MUST be
  query "label:<name>" — e.g. "marketing" -> "label:marketing", "to do" -> "label:to do".
  Do NOT translate "marketing" to "category:promotions". Only use "category:promotions" when the
  user literally says "promotions" or "promotional".
- "send me a briefing/summary every morning at 8" -> manage_routine briefing enabled run_time="08:00".
- "give me a briefing/summary now" or "what needs my attention" -> send_briefing_now.
- Times are 24h "HH:MM". Be conservative: only emit actions you are confident about."""


def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def parse_command(subject: str | None, body: str | None, tz: str | None = None) -> dict:
    """Return {"actions": [...], "summary": str}. Empty actions = not a command."""
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    if subject and subject.strip().startswith(REPLY_SUBJECT_PREFIX):
        return {"actions": [], "summary": ""}

    try:
        now = datetime.now(ZoneInfo(tz or settings.MAILMAN_DEFAULT_TZ))
    except Exception:
        now = datetime.now()
    now_line = f"Current time: {now.isoformat()} ({tz or settings.MAILMAN_DEFAULT_TZ})."

    content = f"{now_line}\nSubject: {subject or '(no subject)'}\n\n{(body or '')[:4000]}"
    resp = _client().chat.completions.create(
        model=settings.OPENAI_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": content},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("commands.bad_json", raw=raw)
        return {"actions": [], "summary": ""}

    actions = data.get("actions")
    if not isinstance(actions, list):
        actions = []
    return {"actions": actions, "summary": data.get("summary") or ""}
