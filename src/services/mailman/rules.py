"""VIP rule evaluation — which messages bypass the hold and deliver immediately."""

import re

# Pull the bare address out of a "Name <addr@dom>" style From header.
_ADDR_RE = re.compile(r"[\w.+-]+@[\w.-]+")


def extract_address(sender: str | None) -> str | None:
    if not sender:
        return None
    m = _ADDR_RE.search(sender)
    return m.group(0).lower() if m else None


def is_vip(
    sender: str | None,
    subject: str | None,
    snippet: str | None,
    *,
    domains: list[str],
    addresses: list[str],
    keywords: list[str],
) -> bool:
    """True if the message should skip the hold (VIP domain/address/keyword)."""
    address = extract_address(sender)
    if address:
        if address in {a.lower() for a in addresses}:
            return True
        domain = address.split("@", 1)[1]
        for d in domains:
            d = d.lower().lstrip("@")
            # exact domain or subdomain match
            if domain == d or domain.endswith("." + d):
                return True

    if keywords:
        haystack = f"{subject or ''} {snippet or ''}".lower()
        for kw in keywords:
            if kw and kw.lower() in haystack:
                return True

    return False
