"""Turning Gmail's wire format into something the rest of the app can read.

Composio handed back a flat message — `sender`, `subject`, `messageText` — and
hid the work. Gmail's own API returns a recursive MIME tree with base64url part
bodies and a header *list*, so that work moves here: one place that walks a
payload, so `_summarize` and the mailbox poller cannot drift apart on what "the
body" means.

Also the other direction: building the RFC 2822 messages that `messages.send`
and `drafts.create` take as a base64url blob.
"""

import base64
import html
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from core.logging import get_logger

log = get_logger(__name__)

# A malformed or hostile message must not be able to hang a sweep, so the walk
# is bounded on both depth and total text.
MAX_DEPTH = 10
MAX_BODY_CHARS = 500_000
# What a non-verbose fetch keeps. Long enough for classification and drafting
# context, short enough that a mailbox-wide sweep doesn't carry megabytes.
PREVIEW_BODY_CHARS = 2000

_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"[ \t]*\n[ \t]*")


def headers(payload: dict) -> dict[str, str]:
    """Header name (lowercased) -> value.

    Gmail returns headers as a list, with inconsistent casing and legitimate
    repeats (`Received` appears once per hop). First occurrence wins, which is
    the right rule for the singleton headers anyone here actually reads.
    """
    out: dict[str, str] = {}
    for header in payload.get("headers") or []:
        name = (header.get("name") or "").lower()
        if name and name not in out:
            out[name] = header.get("value") or ""
    return out


# Recipient fields, in the order a reader would list them. Bcc is included
# because the sender's own copy of a message keeps it, and "did I send this to
# myself" has to be true when the only self-address is there.
_RECIPIENT_HEADERS = ("to", "cc", "bcc")


def canonical_address(value: str) -> str:
    """One address, reduced to what identifies the mailbox.

    Takes either a bare address or a full `Name <addr>` header value: strips
    the display name, lowercases, and drops any `+tag` — so
    `"Nilesh" <Nilesh+inboxos@Chronon.co.in>` and `nilesh@chronon.co.in` compare
    equal. That plus-tag rule is not cosmetic: the assistant sends from the
    user's `+inboxos` alias, so mail addressed to it is mail addressed to them.

    Returns "" for anything without an `@`, which is what group syntax
    (`undisclosed-recipients:;`) and a stray display name parse to.
    """
    address = parseaddr(value)[1].strip().lower()
    local, sep, domain = address.partition("@")
    if not sep or not local or not domain:
        return ""
    return f"{local.split('+', 1)[0]}@{domain}"


def recipient_addresses(header_map: dict[str, str]) -> list[str]:
    """Every canonical address this message was addressed to (To + Cc + Bcc)."""
    # Empty values are dropped before parsing, not after: `getaddresses` is
    # strict from 3.13 on and returns a single empty pair for the whole batch if
    # any element is malformed — and a missing Cc reads as an empty string.
    raw = [value for name in _RECIPIENT_HEADERS if (value := header_map.get(name, "").strip())]
    out: list[str] = []
    for _, address in getaddresses(raw):
        canonical = canonical_address(address)
        if canonical and canonical not in out:
            out.append(canonical)
    return out


def addressed_to(header_map: dict[str, str], *addresses: str | None) -> bool:
    """Whether any of `addresses` is a recipient of this message."""
    recipients = set(recipient_addresses(header_map))
    return any(canonical_address(a) in recipients for a in addresses if a)


def decode_body(part: dict) -> str:
    """Decode one part's body to text.

    `body.data` is base64url **without padding**, which `b64decode` rejects
    outright — hence the manual re-pad. Decoding never raises: this runs across
    whole mailboxes inside list comprehensions, and one message with a mislabeled
    charset must not take down a sweep.
    """
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (ValueError, TypeError):
        return ""

    charset = _charset(part)
    for encoding in (charset, "utf-8"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _charset(part: dict) -> str | None:
    """The charset declared on this part, if any."""
    for header in part.get("headers") or []:
        if (header.get("name") or "").lower() == "content-type":
            value = header.get("value") or ""
            if "charset=" in value.lower():
                charset = value.lower().split("charset=", 1)[1]
                return charset.split(";")[0].strip().strip('"\'') or None
    return None


def strip_html(markup: str) -> str:
    """Crude HTML → text, for messages with no plain-text alternative.

    Not a parser and not trying to be: the output feeds an LLM prompt and a
    preview line, both of which want prose rather than fidelity.
    """
    without_code = _STYLE_RE.sub(" ", markup)
    text = _TAG_RE.sub(" ", without_code)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return _WS_RE.sub("\n", text).strip()


def walk(payload: dict) -> tuple[str, str, list[str]]:
    """Flatten a payload into (plain_text, html_text, attachment_filenames).

    Gmail orders `multipart/alternative` plain-before-html, so the first
    `text/plain` part found in document order is the one to keep.
    """
    plain: list[str] = []
    markup: list[str] = []
    attachments: list[str] = []

    def _visit(part: dict, depth: int) -> None:
        if depth > MAX_DEPTH:
            return

        mime_type = (part.get("mimeType") or "").lower()
        filename = part.get("filename") or ""

        if filename:
            # An attachment, whatever its mime type. Its bytes are never pulled
            # down — only the name is ever shown or counted.
            attachments.append(filename)
            return

        if mime_type.startswith("multipart/"):
            for child in part.get("parts") or []:
                _visit(child, depth + 1)
            return

        if mime_type == "text/plain" and sum(map(len, plain)) < MAX_BODY_CHARS:
            plain.append(decode_body(part))
        elif mime_type == "text/html" and sum(map(len, markup)) < MAX_BODY_CHARS:
            markup.append(decode_body(part))

    _visit(payload, 0)
    return ("\n".join(p for p in plain if p), "\n".join(m for m in markup if m), attachments)


def body_text(payload: dict) -> tuple[str, list[str]]:
    """The readable body and the attachment filenames.

    Prefers `text/plain`; falls back to stripped HTML only when there is no
    plain part anywhere, which is what a marketing email or a Google Docs
    notification typically looks like.
    """
    plain, markup, attachments = walk(payload)
    text = plain.strip() or (strip_html(markup) if markup else "")
    return text, attachments


def message_date(message: dict, header_map: dict[str, str]) -> datetime | None:
    """When the message arrived.

    `internalDate` is Gmail's own receive timestamp in epoch milliseconds and is
    both always present and immune to the sender's clock, so it is preferred
    over the `Date` header.
    """
    internal = message.get("internalDate")
    if internal:
        try:
            return datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            pass

    raw = header_map.get("date")
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        # A `Date` with no zone is naive, and a naive value flowing into
        # comparisons against tz-aware datetimes raises much later, somewhere
        # unrelated. Assume UTC rather than propagate that.
        if parsed is not None and parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


def snippet_of(message: dict, fallback: str = "") -> str:
    """Gmail's own preview line, HTML entities resolved.

    Gmail returns `&#39;` and friends here even though the field is plain text,
    and those go straight into prompts and UI otherwise.
    """
    raw = message.get("snippet") or ""
    text = html.unescape(raw).strip()
    return text or fallback.strip()


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------


def build_message(
    *,
    to: str,
    subject: str,
    body: str,
    from_email: str | None = None,
    is_html: bool = False,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """Build an RFC 2822 message and return it base64url-encoded for Gmail.

    `in_reply_to` / `references` are what make a reply thread in the recipient's
    client. Gmail's own `threadId` only groups the message in *this* mailbox —
    without these headers the other side sees a brand new conversation.
    """
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if from_email:
        message["From"] = from_email
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references

    if is_html:
        # A text/plain alternative first, so the HTML becomes the richer of two
        # views rather than the only one.
        message.set_content(strip_html(body))
        message.add_alternative(body, subtype="html")
    else:
        message.set_content(body)

    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def reply_subject(subject: str | None) -> str:
    """`Re:`-prefix a subject unless it already carries one."""
    text = (subject or "").strip()
    if not text:
        return "Re:"
    return text if text.lower().startswith("re:") else f"Re: {text}"


def build_references(previous_references: str | None, message_id: str | None) -> str | None:
    """Append a Message-ID to a thread's References chain."""
    parts = [previous_references or "", message_id or ""]
    joined = " ".join(part.strip() for part in parts if part and part.strip())
    return joined or None
