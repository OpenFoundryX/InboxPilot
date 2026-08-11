"""Summarize invoices — pull the key details from invoice/receipt mail."""

import json

from openai import OpenAI

from core.config import settings
from core.logging import get_logger
from integrations.google import gmail

log = get_logger(__name__)

QUERY = "(invoice OR receipt OR bill OR statement) -from:me"
_SYS = """From this email, extract billing details. Return ONLY JSON:
{"is_invoice": bool, "vendor": "...", "amount": "...", "due_date": "...", "summary": "<one line>"}.
If it isn't actually an invoice/receipt/bill, return {"is_invoice": false}."""


def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def summarize_invoices(user_id: str, days: int = 30, limit: int = 12) -> tuple[str, str]:
    """Return (subject, body) summarizing recent invoices/receipts."""
    subject = f"🧾 Invoice summary ({days}d)"
    emails = gmail.fetch_by_query(user_id, f"{QUERY} newer_than:{days}d", limit)

    rows: list[str] = []
    for e in emails:
        content = f"From: {e.sender}\nSubject: {e.subject}\n{(e.body or e.snippet or '')[:1500]}"
        try:
            resp = _client().chat.completions.create(
                model=settings.OPENAI_MODEL,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": _SYS}, {"role": "user", "content": content}],
            )
            data = json.loads(resp.choices[0].message.content or "{}")
        except Exception:
            log.exception("invoices.extract_failed", message_id=e.id)
            continue
        if not data.get("is_invoice"):
            continue
        vendor = data.get("vendor") or (e.sender or "").split("<")[0].strip()
        amount = data.get("amount") or "?"
        due = data.get("due_date")
        line = f"  • {vendor}: {amount}"
        if due:
            line += f" — due {due}"
        if data.get("summary"):
            line += f"\n      {data['summary']}"
        rows.append(line)

    if not rows:
        return subject, "No invoices or receipts found in that window.\n\n— InboxOS"

    body = f"Here are the invoices/receipts from the last {days} days:\n\n" + "\n".join(rows)
    body += "\n\n— InboxOS"
    return subject, body
