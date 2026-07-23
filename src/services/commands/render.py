"""Render an assistant reply (lightweight Markdown) into a clean HTML email.

We don't pull in a full Markdown library — the model only ever emits a small,
predictable subset: paragraphs, **bold**, bullet/numbered lists, [label](url)
links, and a final "— InboxOS" sign-off. This renders exactly that subset into
inline-styled HTML that survives Gmail (which strips <head>/<style>), so replies
read like a designed message instead of a wall of plain text.
"""

import html
import re

# Inline styles — Gmail keeps inline styles but drops <style> blocks, so every
# element carries its own.
_BODY = (
    "margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
    "Roboto,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.6;color:#1f2937;"
)
_WRAP = "max-width:560px;margin:0;padding:4px 2px;"
_P = "margin:0 0 12px;"
_UL = "margin:0 0 12px;padding-left:22px;"
_LI = "margin:4px 0;"
_A = "color:#2563eb;text-decoration:none;font-weight:500;"
_STRONG = "color:#111827;font-weight:600;"
_SIG = "margin:18px 0 0;color:#9ca3af;font-size:13px;"

_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_URL_RE = re.compile(r"(?<![\">])(https?://[^\s<]+)")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.*)$")
_SIG_RE = re.compile(r"^\s*[—–-]{1,2}\s*InboxOS\s*$", re.IGNORECASE)


def _inline(text: str) -> str:
    """Format one line: links, bold, autolinked bare URLs — HTML-safe."""
    # Pull markdown links out first (they contain characters we don't want
    # escaped or re-linked), leaving numbered placeholders behind.
    anchors: list[str] = []

    def _stash(m: re.Match) -> str:
        label = html.escape(m.group(1))
        url = html.escape(m.group(2), quote=True)
        anchors.append(f'<a href="{url}" style="{_A}">{label}</a>')
        return f"\x00{len(anchors) - 1}\x00"

    text = _LINK_RE.sub(_stash, text)
    # quote=False: this is a text node, not an attribute, so ' and " are fine.
    text = html.escape(text, quote=False)
    text = _URL_RE.sub(lambda m: f'<a href="{m.group(1)}" style="{_A}">{m.group(1)}</a>', text)
    text = _BOLD_RE.sub(lambda m: f'<strong style="{_STRONG}">{m.group(1)}</strong>', text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: anchors[int(m.group(1))], text)
    return text


def to_html(markdown: str) -> str:
    """Convert the model's Markdown reply into a styled HTML email body."""
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    blocks: list[str] = []
    para: list[str] = []
    items: list[str] = []
    ordered = False

    def flush_para() -> None:
        if para:
            blocks.append(f'<p style="{_P}">' + "<br>".join(para) + "</p>")
            para.clear()

    def flush_list() -> None:
        if items:
            tag = "ol" if ordered else "ul"
            lis = "".join(f'<li style="{_LI}">{it}</li>' for it in items)
            blocks.append(f'<{tag} style="{_UL}">{lis}</{tag}>')
            items.clear()

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_para()
            flush_list()
            continue

        if _SIG_RE.match(line):
            flush_para()
            flush_list()
            blocks.append(f'<p style="{_SIG}">— InboxOS</p>')
            continue

        bullet = _BULLET_RE.match(line)
        ordered_m = _ORDERED_RE.match(line)
        m = bullet or ordered_m
        if m:
            flush_para()
            is_ord = ordered_m is not None
            # A change of list style starts a fresh list.
            if items and is_ord != ordered:
                flush_list()
            ordered = is_ord
            items.append(_inline(m.group(1)))
            continue

        h = _HEADING_RE.match(line)
        if h:
            flush_para()
            flush_list()
            blocks.append(f'<p style="{_P}"><strong style="{_STRONG}">{_inline(h.group(1))}</strong></p>')
            continue

        flush_list()
        para.append(_inline(line))

    flush_para()
    flush_list()

    inner = "\n".join(blocks)
    return f'<div style="{_BODY}"><div style="{_WRAP}">{inner}</div></div>'
