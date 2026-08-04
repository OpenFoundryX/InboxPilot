"""The slash command surface, written down exactly once.

Chat only mutates anything in response to an explicit slash command, so this
table is the whole contract: it decides what `/help` prints, which action types
the parser is allowed to emit for a given command, which commands the intent
classifier may suggest, and what the web autocomplete menu offers. Adding a
command means adding a row here and nothing else.

Two rows deserve a note. `/do` allows every action type — it is the escape
hatch that keeps a strict slash rule from making anything unreachable. And the
four commands carrying a `fixed_action` need no model call at all: `/catchup`
*is* `{"type": "catch_up_now"}`, so asking a model to extract it would only add
latency and a way to get it wrong.
"""

from dataclasses import dataclass

from services.commands.handlers import ACTION_TYPES

# `/help` is resolved before command lookup, so no command may claim this name.
HELP_NAME = "help"


@dataclass(frozen=True)
class SlashCommand:
    """One entry in the command surface.

    `action_types` constrains what the parser may emit; `fixed_action`, when
    set, is emitted directly and the parser is never called.
    """

    name: str
    summary: str
    usage: str
    action_types: tuple[str, ...]
    fixed_action: dict | None = None


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        name="catchup",
        summary="Summarise important unread mail",
        usage="/catchup",
        action_types=("catch_up_now",),
        fixed_action={"type": "catch_up_now"},
    ),
    SlashCommand(
        name="briefing",
        summary="Send a briefing right now",
        usage="/briefing",
        action_types=("send_briefing_now",),
        fixed_action={"type": "send_briefing_now"},
    ),
    SlashCommand(
        name="invoices",
        summary="Summarise recent invoices",
        usage="/invoices",
        action_types=("summarize_invoices_now",),
        fixed_action={"type": "summarize_invoices_now"},
    ),
    SlashCommand(
        name="deadlines",
        summary="Find deadlines and set reminders",
        usage="/deadlines",
        action_types=("scan_deadlines_now",),
        fixed_action={"type": "scan_deadlines_now"},
    ),
    SlashCommand(
        name="remind",
        summary="Set a reminder",
        usage="/remind me tomorrow 3pm to call the bank",
        action_types=("set_reminder",),
    ),
    SlashCommand(
        name="vip",
        summary="Add or remove VIP senders",
        usage="/vip add stripe.com",
        action_types=("add_vip", "remove_vip"),
    ),
    SlashCommand(
        name="rule",
        summary="Create a filing rule",
        usage="/rule archive everything from newsletters@x.com",
        action_types=("create_rule",),
    ),
    SlashCommand(
        name="label",
        summary="Create or delete a Gmail label",
        usage="/label create Receipts",
        action_types=("create_label", "delete_label"),
    ),
    SlashCommand(
        name="routine",
        summary="Turn a scheduled routine on or off",
        usage="/routine briefing every morning at 8",
        action_types=("manage_routine",),
    ),
    SlashCommand(
        name="delivery",
        summary="Change batching, quiet hours, timing",
        usage="/delivery 3 times a day, quiet hours 22:00-07:00",
        action_types=("set_routine",),
    ),
    SlashCommand(
        name="do",
        summary="Anything else",
        usage="/do <what you want>",
        # Sorted so the composed prompt prefix is stable across processes,
        # which matters for prompt caching.
        action_types=tuple(sorted(ACTION_TYPES)),
    ),
)

_BY_NAME = {c.name: c for c in COMMANDS}


def lookup(name: str) -> SlashCommand | None:
    """Find a command by name, case-insensitively. `None` if unknown."""
    return _BY_NAME.get(name.strip().lower())


def help_text() -> str:
    """The `/help` body, as Markdown.

    Each command is a code span so the web renders it as a clickable chip that
    prefills the input — see `Markdown.tsx`.
    """
    lines = ["I make changes only from a slash command. Here's what I take:", ""]
    lines += [f"- `/{c.name}` — {c.summary}" for c in COMMANDS]
    lines += ["", "Type `/` in the message box to pick one, or just start typing a command."]
    return "\n".join(lines)
