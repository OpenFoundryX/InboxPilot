"""Decide whether a chat message opens with a slash command, and which one.

Pure string work by design — no model, no database, no registry mutation. The
chat engine calls this before it classifies anything, so the cheapest possible
answer ("this is a command, and it is this one") never costs a request.
"""

from dataclasses import dataclass

from services.commands.registry import HELP_NAME, SlashCommand, lookup

KIND_NONE = "none"  # ordinary prose; the classifier handles it
KIND_HELP = "help"  # "/" or "/help"
KIND_UNKNOWN = "unknown"  # led with a slash, but no such command
KIND_COMMAND = "command"  # a real command, with `args` after it


@dataclass(frozen=True)
class Resolution:
    kind: str
    command: SlashCommand | None = None
    args: str = ""
    raw_name: str = ""


def resolve(message: str) -> Resolution:
    """Classify `message` against the command surface.

    Only a leading slash counts. A slash anywhere else is ordinary prose — a
    question mentioning a URL path or a subreddit must not be mistaken for a
    command, and users write those often enough to matter.
    """
    text = (message or "").strip()
    if not text.startswith("/"):
        return Resolution(KIND_NONE)

    # Split on the first run of whitespace: everything after it is arguments,
    # kept verbatim because they carry addresses, label names and times.
    body = text[1:]
    name, _, rest = body.partition(" ")
    if "\n" in name:
        name, _, rest = body.partition("\n")

    name = name.strip()
    args = rest.strip()

    if not name or name.lower() == HELP_NAME:
        return Resolution(KIND_HELP)

    command = lookup(name)
    if command is None:
        return Resolution(KIND_UNKNOWN, raw_name=name)

    return Resolution(KIND_COMMAND, command=command, args=args)
