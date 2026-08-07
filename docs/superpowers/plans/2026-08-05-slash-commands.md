# Slash Commands in Chat — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a state change in chat reachable only through an explicit slash command, so a misfiring LLM intent classifier can no longer produce a confirm card with nothing to approve.

**Architecture:** A new `registry.py` becomes the single source of truth for the command surface, feeding the parser's prompt composition, the intent classifier's prompt, `/help`, and the web menu. `parser.py`'s one monolithic system prompt splits into per-action-type specs so a slash can constrain the model to a subset. `engine.turn_events` gains a slash branch that short-circuits before any classification, and loses the branch where `INTENT_COMMAND` called `parse_command`. Prose that reads as a command gets answered normally, then a suggestion is appended as ordinary Markdown text — so nothing new is persisted and no migration is needed.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (async), OpenAI SDK, pytest + pytest-asyncio. Frontend: Next.js 14 App Router, React 18, TypeScript 5.5, Tailwind 3.4.

**Spec:** `docs/superpowers/specs/2026-08-05-slash-commands-design.md`

## Global Constraints

- **Two repos.** Backend is `/Users/abcom/Desktop/openfoundry/InboxPilot`. Frontend is `/Users/abcom/Desktop/openfoundry/inboxos-web`. They are separate git repos — commit in each separately. Tasks 1–6 are backend, 7–11 are frontend.
- **No Alembic migration.** No task in this plan adds, drops, or alters a column. If you find yourself writing one, stop — you have misread the design.
- **The email surface must not change behaviour.** `parse_command(subject, body, tz)` with no fourth argument must compose the exact same prompt it composes today, because `src/workers/jobs/handle_command_email.py` calls it that way. Task 2 has an explicit test for this.
- **Python line length is 100** (`[tool.ruff] line-length = 100`).
- **Backend tests live in `tests/`**, mirroring `src/` (`tests/services/commands/`, `tests/services/chat/`). `pyproject.toml` already sets `testpaths = ["tests"]` and `pythonpath = ["src"]`, so imports in tests are `from services.commands import registry` — no `src.` prefix.
- **Every backend test in this plan is a pure unit test.** No database, no network, no OpenAI call. Do not create a `tests/conftest.py` with a database fixture; nothing here needs one.
- **`inboxos-web` has no test runner** (no jest, no vitest, and adding one is out of scope). Frontend tasks verify with `npx tsc --noEmit`, `npm run lint`, and the stated manual browser check.
- **Command copy is fixed.** The `name`, `summary`, and `usage` strings in Task 1 are the approved surface. Do not reword them in later tasks — the web menu, `/help`, and the classifier prompt all render these exact strings.

---

## File Structure

**Backend — create:**

| File | Responsibility |
|---|---|
| `src/services/commands/registry.py` | The command surface. Dataclass, the eleven commands, lookup, `/help` text. No I/O. |
| `src/services/commands/slash.py` | Pure string parsing: does this message start a command, and which one. No I/O. |
| `tests/services/commands/test_registry.py` | Drift guard between the registry and `handlers.execute`. |
| `tests/services/commands/test_slash.py` | Every resolution rule. |
| `tests/services/commands/test_parser.py` | Prompt composition and out-of-scope filtering. |
| `tests/services/chat/test_engine.py` | One test per routing branch, with fakes. |

**Backend — modify:**

| File | Change |
|---|---|
| `src/services/commands/handlers.py` | Add `ACTION_TYPES` frozenset next to `execute`. No behaviour change. |
| `src/services/commands/parser.py` | Split `_SYSTEM` into `_ACTION_SPECS` + `_GLOBAL_RULES`; add `build_system`; add `allowed_types`. |
| `src/services/chat/intent.py` | Return an `Intent` dataclass carrying the suggested command; registry-driven prompt. |
| `src/services/chat/engine.py` | Slash branch at the top; delete the `INTENT_COMMAND` → `parse_command` branch; append the nudge. |
| `src/api/v1/chat.py` | Add `GET /chat/commands`. |
| `src/schemas/chat.py` | Add `CommandRead`. |

**Frontend — create:**

| File | Responsibility |
|---|---|
| `src/components/chat/SlashMenu.tsx` | The filtered popup list above the input. Presentational. |
| `src/components/chat/PrefillContext.tsx` | Carries the command list and a prefill callback down to `Markdown`. |

**Frontend — modify:**

| File | Change |
|---|---|
| `src/lib/chat.ts` | `SlashCommandInfo` type + `listCommands()`. |
| `src/components/app/AskBar.tsx` | Optional controlled value; optional slash menu. |
| `src/components/app/icons.tsx` | `WarnIcon`, `XIcon`. |
| `src/components/chat/Markdown.tsx` | Inline code spans, rendered as prefill chips when they name a command. |
| `src/components/chat/ActionConfirm.tsx` | Restyle: warning title, Deny/Approve footer. |
| `src/app/dashboard/chat/page.tsx` | Draft state, context provider, empty-state hint. |

---

## Task 0: Environment check

**Files:** none.

- [ ] **Step 1: Install dev dependencies**

`ruff` and `mypy` are declared in the `dev` extra but may not be in your `.venv`. Run:

```bash
cd /Users/abcom/Desktop/openfoundry/InboxPilot
make install
```

- [ ] **Step 2: Confirm the toolchain runs**

```bash
uv run pytest --version && uv run ruff --version
```

Expected: both print a version. If `ruff` still fails to spawn, re-run `uv sync --extra dev` before continuing — every backend task ends with a lint step.

---

## Task 1: The command registry

**Files:**
- Create: `src/services/commands/registry.py`
- Create: `tests/services/commands/test_registry.py`
- Modify: `src/services/commands/handlers.py` (add `ACTION_TYPES` near the top-level constants, around line 44)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `SlashCommand` frozen dataclass with fields `name: str`, `summary: str`, `usage: str`, `action_types: tuple[str, ...]`, `fixed_action: dict | None = None`
  - `COMMANDS: tuple[SlashCommand, ...]`
  - `HELP_NAME: str = "help"`
  - `lookup(name: str) -> SlashCommand | None` — case-insensitive
  - `help_text() -> str` — Markdown
  - `handlers.ACTION_TYPES: frozenset[str]`

- [ ] **Step 1: Write the failing test**

Create `tests/services/commands/test_registry.py`. The `__init__.py` files are not needed — pytest's rootdir config handles discovery.

```python
"""The registry is the only place the command surface is written down.

The drift guard is the point of this file: `handlers.execute` grew a
twelfth action type once already, and nothing would have noticed if a
thirteenth arrived with no way to reach it under a slash-only rule.
"""

import inspect
import re

from services.commands import handlers, registry


def test_names_are_unique_and_lowercase():
    names = [c.name for c in registry.COMMANDS]
    assert len(names) == len(set(names))
    assert all(n == n.lower() and n.isalpha() for n in names)


def test_help_is_not_a_command_name():
    # `/help` is resolved before lookup; a command named "help" would shadow it.
    assert registry.lookup(registry.HELP_NAME) is None


def test_lookup_is_case_insensitive():
    assert registry.lookup("VIP") is registry.lookup("vip")
    assert registry.lookup("nope") is None


def test_usage_starts_with_its_own_name():
    for c in registry.COMMANDS:
        assert c.usage.startswith(f"/{c.name}"), c.name


def test_fixed_actions_declare_exactly_their_own_type():
    for c in registry.COMMANDS:
        if c.fixed_action is None:
            continue
        assert c.action_types == (c.fixed_action["type"],), c.name


def test_every_registered_type_is_executable():
    registered = {t for c in registry.COMMANDS for t in c.action_types}
    assert registered <= handlers.ACTION_TYPES


def test_every_executable_type_is_reachable_from_a_named_command():
    # `/do` allows everything, so it would trivially satisfy this. Exclude it:
    # the point is that each type has a *discoverable* home in the menu.
    named = {t for c in registry.COMMANDS if c.name != "do" for t in c.action_types}
    assert named == handlers.ACTION_TYPES


def test_do_allows_every_type():
    do = registry.lookup("do")
    assert do is not None
    assert set(do.action_types) == handlers.ACTION_TYPES


def test_action_types_constant_matches_what_execute_dispatches_on():
    """Keeps `ACTION_TYPES` honest against the if-chain it sits beside."""
    source = inspect.getsource(handlers.execute)
    dispatched = set(re.findall(r'atype\s*(?:==|in)\s*\(?\s*"([^"]+)"', source))
    dispatched |= set(re.findall(r'atype\s+in\s+\("[^"]+",\s*"([^"]+)"', source))
    assert dispatched == handlers.ACTION_TYPES


def test_help_text_lists_every_command_as_a_code_span():
    text = registry.help_text()
    for c in registry.COMMANDS:
        assert f"`/{c.name}`" in text
        assert c.summary in text
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd /Users/abcom/Desktop/openfoundry/InboxPilot
uv run pytest tests/services/commands/test_registry.py -v
```

Expected: collection error — `ImportError: cannot import name 'registry' from 'services.commands'`.

- [ ] **Step 3: Add `ACTION_TYPES` to handlers.py**

In `src/services/commands/handlers.py`, immediately after the `_NOW_ACTION_ROUTINES` dict (it ends around line 58, before `_ROUTINE_FIELDS`), insert:

```python
# Every action type `execute` below dispatches on. Declared here rather than
# derived, so `services.commands.registry` can be checked against it without
# importing the handler's dependencies — and so a new type added to `execute`
# without a slash command to reach it fails a test instead of going unnoticed.
ACTION_TYPES = frozenset(
    {
        "create_label",
        "delete_label",
        "set_routine",
        "add_vip",
        "remove_vip",
        "create_rule",
        "manage_routine",
        "send_briefing_now",
        "catch_up_now",
        "summarize_invoices_now",
        "scan_deadlines_now",
        "set_reminder",
    }
)
```

- [ ] **Step 4: Write the registry**

Create `src/services/commands/registry.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/services/commands/test_registry.py -v
```

Expected: 10 passed.

- [ ] **Step 6: Lint**

```bash
uv run ruff check src/services/commands/registry.py src/services/commands/handlers.py tests
uv run ruff format --check src/services/commands/registry.py tests
```

Expected: clean. If `format --check` complains, run `uv run ruff format` on the named paths.

- [ ] **Step 7: Commit**

```bash
git add src/services/commands/registry.py src/services/commands/handlers.py tests/services/commands/test_registry.py
git commit -m "feat: add the slash command registry

One table drives /help, the parser's allowed action types, the intent
classifier's prompt and the web menu. A drift guard test asserts every
type handlers.execute dispatches on is reachable from a named command."
```

---

## Task 2: Constrained parsing

**Files:**
- Modify: `src/services/commands/parser.py` (replace `_SYSTEM` at lines 26–77; change `parse_command` at lines 84–118)
- Create: `tests/services/commands/test_parser.py`

**Interfaces:**
- Consumes: `registry.COMMANDS` (only for ordering intuition; no import needed).
- Produces:
  - `build_system(types: tuple[str, ...] | None) -> str`
  - `parse_command(subject: str | None, body: str | None, tz: str | None = None, allowed_types: tuple[str, ...] | None = None) -> dict` returning `{"actions": list[dict], "summary": str}`
  - `_ACTION_SPECS: dict[str, str]` (module-private; the test imports it deliberately)

- [ ] **Step 1: Write the failing test**

Create `tests/services/commands/test_parser.py`:

```python
"""Prompt composition and the out-of-scope filter.

No OpenAI call happens here: `build_system` is pure, and the filtering test
replaces the client with a stub that returns a fixed JSON body.
"""

import json

import pytest

from services.commands import parser
from services.commands.handlers import ACTION_TYPES


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeClient:
    """Stands in for `OpenAI`, capturing the system prompt it was handed."""

    def __init__(self, payload):
        # A str payload is returned verbatim, so a test can feed it junk.
        self.content = payload if isinstance(payload, str) else json.dumps(payload)
        self.system_prompt = None
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.system_prompt = kwargs["messages"][0]["content"]
        return _FakeResponse(self.content)


@pytest.fixture
def fake_openai(monkeypatch):
    """Install a fake client and hand the test a way to set its reply."""

    holder = {}

    def install(payload):
        client = _FakeClient(payload)
        holder["client"] = client
        monkeypatch.setattr(parser, "_client", lambda: client)
        monkeypatch.setattr(parser.settings, "OPENAI_API_KEY", "test-key")
        return client

    return install


def test_specs_cover_every_executable_type():
    assert set(parser._ACTION_SPECS) == ACTION_TYPES


def test_build_system_with_none_includes_every_type():
    prompt = parser.build_system(None)
    for atype in ACTION_TYPES:
        assert f'"type": "{atype}"' in prompt


def test_build_system_subset_excludes_the_others():
    prompt = parser.build_system(("create_rule",))
    assert '"type": "create_rule"' in prompt
    assert '"type": "set_routine"' not in prompt
    assert '"type": "add_vip"' not in prompt


def test_build_system_always_includes_the_global_rules():
    for types in (None, ("create_rule",), ("set_reminder",)):
        assert "NEVER output placeholder values" in parser.build_system(types)


def test_build_system_ordering_is_stable():
    a = parser.build_system(("create_rule", "add_vip"))
    b = parser.build_system(("add_vip", "create_rule"))
    assert a == b


def test_parse_command_default_composes_every_type(fake_openai):
    """The email-to-self path calls with three arguments and must not change."""
    client = fake_openai({"actions": [], "summary": ""})
    parser.parse_command("Subject", "body", "UTC")
    assert client.system_prompt == parser.build_system(None)


def test_parse_command_passes_only_the_allowed_types(fake_openai):
    client = fake_openai({"actions": [], "summary": ""})
    parser.parse_command(None, "archive stuff", "UTC", allowed_types=("create_rule",))
    assert client.system_prompt == parser.build_system(("create_rule",))


def test_out_of_scope_actions_are_dropped(fake_openai):
    fake_openai(
        {
            "actions": [
                {"type": "create_rule", "criteria": {"from": "x@y.com"}, "archive": True},
                {"type": "set_routine", "times_per_day": 3},
            ],
            "summary": "archive x",
        }
    )
    out = parser.parse_command(None, "archive x", "UTC", allowed_types=("create_rule",))
    assert [a["type"] for a in out["actions"]] == ["create_rule"]
    assert out["summary"] == "archive x"


def test_no_filtering_when_allowed_types_is_none(fake_openai):
    fake_openai(
        {"actions": [{"type": "set_routine", "times_per_day": 3}], "summary": "batch"}
    )
    out = parser.parse_command("s", "b", "UTC")
    assert [a["type"] for a in out["actions"]] == ["set_routine"]


def test_malformed_json_returns_no_actions(fake_openai):
    fake_openai("not json at all")
    out = parser.parse_command(None, "anything", "UTC")
    assert out == {"actions": [], "summary": ""}


def test_reply_prefix_still_short_circuits(fake_openai):
    fake_openai({"actions": [{"type": "create_label", "name": "X"}], "summary": "x"})
    out = parser.parse_command(f"{parser.REPLY_SUBJECT_PREFIX} done", "body", "UTC")
    assert out == {"actions": [], "summary": ""}
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/services/commands/test_parser.py -v
```

Expected: FAIL — `AttributeError: module 'services.commands.parser' has no attribute '_ACTION_SPECS'`.

- [ ] **Step 3: Replace `_SYSTEM` with per-type specs**

In `src/services/commands/parser.py`, delete the whole `_SYSTEM = f"""..."""` block (lines 26–77) and put this in its place. Keep the `REPLY_SUBJECT_PREFIX` constant above it exactly as it is.

```python
# Each action type's JSON shape *and the rules that only concern that type*.
# Keeping them together is what makes a constrained prompt possible: `/rule`
# composes six lines instead of fifty, so the model has no `set_routine` shape
# in front of it to reach for when the user asked to file mail.
_ACTION_SPECS: dict[str, str] = {
    "create_label": """- {"type": "create_label", "name": "Receipts"}""",
    "delete_label": """- {"type": "delete_label", "name": "OldLabel"}""",
    "set_routine": """- {"type": "set_routine", "delivery_mode": "interval|times|custom_daily",
     "interval_minutes": int, "times_per_day": int, "custom_times": ["09:00","18:00"],
     "active_window_start": "09:00", "active_window_end": "21:00",
     "dnd_enabled": bool, "dnd_start": "22:00", "dnd_end": "07:00", "timezone": "Asia/Kolkata"}
  * "deliver N times a day" -> delivery_mode=times, times_per_day=N.
  * "deliver at 1pm and 6pm" -> delivery_mode=custom_daily, custom_times=["13:00","18:00"].
  * "every N minutes/hours" -> delivery_mode=interval with interval_minutes.""",
    "add_vip": """- {"type": "add_vip", "domains": ["stripe.com"], "addresses": ["a@b.com"], "keywords": ["OTP"]}""",
    "remove_vip": """- {"type": "remove_vip", "domains": [...], "addresses": [...], "keywords": [...]}""",
    "create_rule": f"""- {{"type": "create_rule", "criteria": {{"from": "x@y.com"}}, "archive": true, "apply_to_existing": true}}
     (criteria may use from/to/subject/query; also optional: apply_label, archive, star, mark_read, trash;
      set apply_to_existing=true to ALSO apply the effect to mail already in the mailbox, not just future mail)
  * Only set apply_label when the user explicitly asks to label; do not invent one.
  * If asked to auto-label, apply_label should be one of: {", ".join(_GMAIL_LABELS)} (or a label the user names).
  * "archive"/"skip inbox" -> archive=true. "delete"/"trash" -> trash=true.
  * If the user says "current"/"existing"/"already"/"all my ... (that are already here)",
    ALSO set apply_to_existing=true so mail already in the mailbox is acted on, not just future mail.
    "archive current AND future X" is a SINGLE create_rule with archive=true and apply_to_existing=true.
  * The user's own labels are: {", ".join(_GMAIL_LABELS)}. These are labels, NOT Gmail categories.
    When the user names one of them (e.g. "marketing emails", "my fyi mail"), the criteria MUST be
    query "label:<name>" — e.g. "marketing" -> "label:marketing", "to do" -> "label:to do".
    Do NOT translate "marketing" to "category:promotions". Only use "category:promotions" when the
    user literally says "promotions" or "promotional".""",
    "manage_routine": """- {"type": "manage_routine", "routine": "briefing", "enabled": true, "run_time": "08:00", "weekday": null}
     (routine is one of: "briefing" (daily digest), "chase_threads" (nudge threads awaiting a reply),
      "reconnect" (people to reach out to), "deadline_scan" (turn deadlines into reminders),
      "catchup" (important unread), "invoices" (invoice summary),
      "double_bookings" (calendar clash heads-up), "schedule_trusted" (draft meeting times for VIP requests);
      weekday 0=Mon..6=Sun for weekly, null=daily; set enabled=false to turn off)
  * "send me a briefing/summary every morning at 8" -> briefing enabled run_time="08:00".""",
    "send_briefing_now": """- {"type": "send_briefing_now"}  (send a briefing/summary email immediately)
  * "give me a briefing/summary now" or "what needs my attention" -> send_briefing_now.""",
    "catch_up_now": """- {"type": "catch_up_now"}  (summarize important unread mail — "catch me up", "what did I miss")""",
    "summarize_invoices_now": """- {"type": "summarize_invoices_now"}  (summarize recent invoices/receipts/bills)""",
    "scan_deadlines_now": """- {"type": "scan_deadlines_now"}  (find deadlines in recent mail and set reminders)""",
    "set_reminder": """- {"type": "set_reminder", "remind_at": "2026-07-23T15:00:00+05:30", "title": "Call the bank", "note": "..."}
     (remind_at is an ISO 8601 datetime WITH the user's timezone offset; resolve relative times
      like "tomorrow 3pm" / "in 2 hours" against the current time given below)""",
}

_PREAMBLE = """You convert a user's instruction into structured commands for their
email assistant. Return ONLY JSON: {"actions": [...], "summary": "<short human summary>"}.

If the instruction contains nothing actionable, return {"actions": [], "summary": ""}.

Each action is one of these shapes (include only the fields that apply):"""

_GLOBAL_RULES = """Rules:
- Include ONLY the fields that apply. OMIT every unused field entirely.
- NEVER output placeholder values like "..." — if you don't have a value, leave the field out.
- Times are 24h "HH:MM". Be conservative: only emit actions you are confident about."""


def build_system(types: tuple[str, ...] | None = None) -> str:
    """Compose a system prompt covering exactly `types`.

    `None` means every type, which is what the email-to-self surface uses —
    there, any message is a candidate command and nothing narrows it down. A
    slash command passes its own `action_types` instead.

    Specs are emitted in `_ACTION_SPECS` insertion order regardless of the
    order `types` arrives in, so the prompt prefix is byte-stable and stays
    cacheable.
    """
    wanted = set(_ACTION_SPECS) if types is None else set(types)
    specs = [spec for atype, spec in _ACTION_SPECS.items() if atype in wanted]
    return f"{_PREAMBLE}\n" + "\n".join(specs) + f"\n\n{_GLOBAL_RULES}"
```

Note the `_GMAIL_LABELS` f-string interpolations inside `create_rule` — `_GMAIL_LABELS` is already defined at line 20 of the file, above this block. Leave it there.

- [ ] **Step 4: Add `allowed_types` to `parse_command`**

Replace the `parse_command` function body's prompt selection and return. The full replacement for lines 84–118:

```python
def parse_command(
    subject: str | None,
    body: str | None,
    tz: str | None = None,
    allowed_types: tuple[str, ...] | None = None,
) -> dict:
    """Return {"actions": [...], "summary": str}. Empty actions = not a command.

    `allowed_types` narrows both the prompt and the result. A constrained
    prompt still lets a model reach for a shape it wasn't given, so the
    returned actions are filtered too rather than trusted.
    """
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
            {"role": "system", "content": build_system(allowed_types)},
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

    if allowed_types is not None:
        allowed = set(allowed_types)
        kept = [a for a in actions if isinstance(a, dict) and a.get("type") in allowed]
        if len(kept) != len(actions):
            dropped = [a.get("type") for a in actions if isinstance(a, dict)]
            log.warning("commands.action_out_of_scope", allowed=sorted(allowed), got=dropped)
        actions = kept

    return {"actions": actions, "summary": data.get("summary") or ""}
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
uv run pytest tests/services/commands/test_parser.py -v
```

Expected: 11 passed.

- [ ] **Step 6: Verify the email path is untouched**

```bash
uv run python -c "
import sys; sys.path.insert(0, 'src')
from services.commands.parser import build_system
from services.commands.handlers import ACTION_TYPES
p = build_system(None)
missing = [t for t in ACTION_TYPES if f'\"type\": \"{t}\"' not in p]
print('missing types:', missing)
assert not missing
print('OK — default prompt covers all', len(ACTION_TYPES), 'types')
"
```

Expected: `OK — default prompt covers all 12 types`.

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check src/services/commands/parser.py tests
git add src/services/commands/parser.py tests/services/commands/test_parser.py
git commit -m "refactor: compose the command prompt from per-type specs

parse_command gains allowed_types, which narrows both the prompt and the
returned actions. Passing None composes every type, so the email-to-self
path is unchanged."
```

---

## Task 3: Slash resolution

**Files:**
- Create: `src/services/commands/slash.py`
- Create: `tests/services/commands/test_slash.py`

**Interfaces:**
- Consumes: `registry.lookup`, `registry.HELP_NAME`, `registry.SlashCommand`.
- Produces:
  - `Resolution` frozen dataclass: `kind: str`, `command: SlashCommand | None`, `args: str`, `raw_name: str`
  - Kind constants `KIND_NONE = "none"`, `KIND_HELP = "help"`, `KIND_UNKNOWN = "unknown"`, `KIND_COMMAND = "command"`
  - `resolve(message: str) -> Resolution`

- [ ] **Step 1: Write the failing test**

Create `tests/services/commands/test_slash.py`:

```python
"""Every resolution rule, including the ones that look like edge cases and
are actually the common ways a user gets this wrong."""

from services.commands import slash


def test_prose_is_not_a_command():
    r = slash.resolve("show me my important emails")
    assert r.kind == slash.KIND_NONE
    assert r.command is None


def test_a_slash_later_in_the_message_is_prose():
    r = slash.resolve("what about /r/email as a source")
    assert r.kind == slash.KIND_NONE


def test_leading_whitespace_still_resolves():
    r = slash.resolve("   /vip add stripe.com")
    assert r.kind == slash.KIND_COMMAND
    assert r.command.name == "vip"
    assert r.args == "add stripe.com"


def test_bare_slash_is_help_not_unknown():
    assert slash.resolve("/").kind == slash.KIND_HELP


def test_help_is_help():
    assert slash.resolve("/help").kind == slash.KIND_HELP
    assert slash.resolve("/HELP").kind == slash.KIND_HELP
    assert slash.resolve("/help  ").kind == slash.KIND_HELP


def test_names_match_case_insensitively():
    assert slash.resolve("/VIP add x@y.com").command.name == "vip"


def test_args_keep_their_original_case():
    # Args carry addresses and label names; lowercasing them corrupts both.
    r = slash.resolve("/label create Receipts From AWS")
    assert r.args == "create Receipts From AWS"


def test_command_with_no_args_has_empty_args():
    r = slash.resolve("/catchup")
    assert r.kind == slash.KIND_COMMAND
    assert r.command.name == "catchup"
    assert r.args == ""


def test_trailing_whitespace_after_a_bare_command_is_not_args():
    assert slash.resolve("/catchup   ").args == ""


def test_unknown_command_reports_the_name_it_saw():
    r = slash.resolve("/sdfsd do a thing")
    assert r.kind == slash.KIND_UNKNOWN
    assert r.raw_name == "sdfsd"
    assert r.command is None


def test_empty_message_is_not_a_command():
    assert slash.resolve("").kind == slash.KIND_NONE
    assert slash.resolve("   ").kind == slash.KIND_NONE


def test_multiline_args_are_preserved():
    r = slash.resolve("/do archive x\nand star y")
    assert r.kind == slash.KIND_COMMAND
    assert r.args == "archive x\nand star y"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/services/commands/test_slash.py -v
```

Expected: collection error — no module named `services.commands.slash`.

- [ ] **Step 3: Write the resolver**

Create `src/services/commands/slash.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/services/commands/test_slash.py -v
```

Expected: 12 passed.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/services/commands/slash.py tests
git add src/services/commands/slash.py tests/services/commands/test_slash.py
git commit -m "feat: resolve leading slash commands

Only a leading slash counts, names match case-insensitively, and args keep
their case because they carry addresses and label names."
```

---

## Task 4: Intent classifier returns a suggestion

**Files:**
- Modify: `src/services/chat/intent.py` (whole file)
- Create: `tests/services/chat/test_intent.py`

**Interfaces:**
- Consumes: `registry.COMMANDS`.
- Produces:
  - `Intent` frozen dataclass: `kind: str`, `command: str | None`
  - `classify(message: str, history: list[dict] | None = None) -> Intent`
  - Existing constants `INTENT_SMALLTALK`, `INTENT_QUESTION`, `INTENT_COMMAND` keep their current string values.

**Note:** `classify`'s return type changes from `str` to `Intent`. Task 5 updates the only caller (`engine._intent`). Nothing else in the repo imports it — verify with `grep -rn "from services.chat.intent import\|chat.intent" src/` before starting.

- [ ] **Step 1: Write the failing test**

Create `tests/services/chat/test_intent.py`:

```python
"""The classifier's new job: it suggests, it no longer decides.

Its output can no longer cause a state change, so these tests are about the
suggestion staying inside the command surface — a suggested `/frobnicate`
would render as a chip that does nothing.
"""

import json

import pytest

from services.chat import intent
from services.commands import registry


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeClient:
    def __init__(self, content):
        self.content = content
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        return _FakeResponse(self.content)


@pytest.fixture
def reply(monkeypatch):
    def install(payload):
        content = payload if isinstance(payload, str) else json.dumps(payload)
        monkeypatch.setattr(intent, "_client", lambda: _FakeClient(content))
        monkeypatch.setattr(intent.settings, "OPENAI_API_KEY", "test-key")

    return install


def test_question_carries_no_command(reply):
    reply({"intent": "question"})
    result = intent.classify("what did I miss?")
    assert result.kind == intent.INTENT_QUESTION
    assert result.command is None


def test_smalltalk_carries_no_command(reply):
    reply({"intent": "smalltalk", "command": "rule"})
    result = intent.classify("who are you?")
    assert result.kind == intent.INTENT_SMALLTALK
    assert result.command is None


def test_command_carries_the_suggested_name(reply):
    reply({"intent": "command", "command": "rule"})
    result = intent.classify("archive all the marketing emails")
    assert result.kind == intent.INTENT_COMMAND
    assert result.command == "rule"


def test_unknown_suggestion_falls_back_to_do(reply):
    reply({"intent": "command", "command": "frobnicate"})
    assert intent.classify("do something odd").command == "do"


def test_missing_suggestion_falls_back_to_do(reply):
    reply({"intent": "command"})
    assert intent.classify("do something odd").command == "do"


def test_malformed_json_degrades_to_question(reply):
    reply("this is not json")
    result = intent.classify("anything")
    assert result.kind == intent.INTENT_QUESTION
    assert result.command is None


def test_unknown_intent_degrades_to_question(reply):
    reply({"intent": "banana"})
    assert intent.classify("anything").kind == intent.INTENT_QUESTION


def test_prompt_lists_the_real_command_surface():
    for c in registry.COMMANDS:
        assert c.name in intent.SYSTEM
        assert c.summary in intent.SYSTEM


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.setattr(intent.settings, "OPENAI_API_KEY", "")
    with pytest.raises(RuntimeError):
        intent.classify("anything")
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/services/chat/test_intent.py -v
```

Expected: FAIL — `AttributeError: module 'services.chat.intent' has no attribute 'SYSTEM'`, and `classify` returning a `str` has no `.kind`.

- [ ] **Step 3: Rewrite intent.py**

Replace the entire contents of `src/services/chat/intent.py`:

```python
"""Decide what a chat message is asking for — and, if it wants a change, which
slash command would make it.

This module used to sit on the path to a state change: `INTENT_COMMAND` went
straight to `parse_command`, which raised a confirm card. That put a
probabilistic call in front of an irreversible one, and when it guessed wrong
the user got a card with nothing to approve and no answer either.

Chat now mutates nothing without an explicit slash command, so this classifier
has a smaller and much more forgiving job: it decides whether to *suggest*. A
wrong `command` costs the user one sentence beneath a real answer.

The command surface is injected from `services.commands.registry`, so the model
is picking from the same list `/help` prints and the web menu offers — it
cannot invent a command that does not exist, and `_suggestion` drops it to
`/do` if it tries anyway.
"""

import json
from dataclasses import dataclass

from openai import OpenAI

from core.config import settings
from core.logging import get_logger
from services.commands.registry import COMMANDS, lookup

log = get_logger(__name__)

# About the assistant or the conversation; answerable without the mailbox.
INTENT_SMALLTALK = "smalltalk"
# Wants to read/find/summarise mail. Nothing in the account changes.
INTENT_QUESTION = "question"
# Wants something changed. We do not change it — we suggest the slash command.
INTENT_COMMAND = "command"

_INTENTS = {INTENT_SMALLTALK, INTENT_QUESTION, INTENT_COMMAND}

# The escape hatch, used when the model names a command that doesn't exist.
_FALLBACK_COMMAND = "do"

# How many prior turns the classifier sees. Enough to resolve a follow-up
# ("do it at 9am instead") without paying for the whole transcript.
_HISTORY_TURNS = 4

_COMMAND_LIST = "\n".join(f"- {c.name}: {c.summary}" for c in COMMANDS)

SYSTEM = f"""You route one message in a chat between a user and InboxOS, their email
assistant. Return ONLY JSON: {{"intent": "...", "command": "..." | null}}.

"intent" is one of:
- "{INTENT_SMALLTALK}" — about the assistant or the conversation rather than the
  mailbox: greetings, "who are you", "what can you do", "how does batching work",
  thanks, chit-chat. Anything answerable without opening their mailbox.
- "{INTENT_QUESTION}" — the user wants to READ, FIND, SEE, COUNT or SUMMARISE mail.
  Examples: "show me my important emails", "what did I miss?", "any invoices from
  AWS?", "did Pradeep reply?", "catch me up", "summarise this week".
- "{INTENT_COMMAND}" — the user wants something CHANGED or DONE: create/delete a
  label, add or remove a VIP, create a rule, change the delivery routine or quiet
  hours, turn a scheduled routine on or off, set a reminder, archive/star/trash/label
  mail, or send them an email right now.

"command" is required when intent is "{INTENT_COMMAND}", and null otherwise. It names
the slash command that would carry out what they asked, chosen from EXACTLY this list:
{_COMMAND_LIST}

Use "do" when the request is a real change but no other name fits.

Rules:
- Showing something IN THIS CHAT is "{INTENT_QUESTION}", never "{INTENT_COMMAND}".
- show/find/list/see/what/which/who/did/any/how many/summarise/catch me up -> question.
- make/create/delete/add/remove/stop/start/turn on/turn off/always/from now on/set/
  schedule/remind me/archive/trash/mute/send me -> command.
- A question ABOUT a capability ("can you batch my mail?") is "{INTENT_SMALLTALK}"; an
  instruction to use it ("batch my mail") is "{INTENT_COMMAND}".
- When torn between question and command, answer "{INTENT_QUESTION}"."""


@dataclass(frozen=True)
class Intent:
    """What the message wants, and which command would deliver it.

    `command` is a registry command name, set only when `kind` is
    `INTENT_COMMAND`. It is a suggestion the user must act on — nothing
    downstream executes it.
    """

    kind: str
    command: str | None = None


def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def _suggestion(raw: object) -> str:
    """Coerce the model's command name onto the real surface."""
    if isinstance(raw, str) and lookup(raw) is not None:
        return lookup(raw).name
    log.info("chat.intent_command_unmapped", got=raw)
    return _FALLBACK_COMMAND


def classify(message: str, history: list[dict] | None = None) -> Intent:
    """Return the `Intent` for this message.

    Falls back to `INTENT_QUESTION` on anything unexpected — a malformed
    response should cost the user an answer they didn't need, not a suggestion
    they can't use.
    """
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    turns = [
        f"{h.get('role', 'user')}: {(h.get('content') or '')[:300]}"
        for h in (history or [])[-_HISTORY_TURNS:]
        if h.get("content")
    ]
    context = "Earlier in this chat:\n" + "\n".join(turns) + "\n\n" if turns else ""

    resp = _client().chat.completions.create(
        model=settings.OPENAI_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"{context}Classify this message:\n{message[:2000]}"},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("chat.intent_bad_json", raw=raw)
        return Intent(INTENT_QUESTION)

    kind = data.get("intent")
    if kind not in _INTENTS:
        log.warning("chat.intent_unknown", intent=kind)
        return Intent(INTENT_QUESTION)

    if kind != INTENT_COMMAND:
        return Intent(kind)
    return Intent(kind, _suggestion(data.get("command")))
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/services/chat/test_intent.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Confirm nothing else consumed the old return type**

```bash
grep -rn "chat.intent\|from services.chat import intent" src/
```

Expected: only `src/services/chat/engine.py`. Task 5 fixes it — the repo is briefly inconsistent between these two commits, which is why they are adjacent.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src/services/chat/intent.py tests
git add src/services/chat/intent.py tests/services/chat/test_intent.py
git commit -m "feat: intent classifier suggests a slash command

classify returns an Intent carrying the command name, picked from the
registry in the same request, so the nudge costs no extra model call. An
unmapped name falls back to /do."
```

---

## Task 5: Engine routing

**Files:**
- Modify: `src/services/chat/engine.py` (imports at lines 22–39; `_intent` at 150–163; `turn_events` at 174–232)
- Create: `tests/services/chat/test_engine.py`

**Interfaces:**
- Consumes: `slash.resolve`, `slash.KIND_*`, `registry.help_text`, `parser.parse_command(..., allowed_types=)`, `intent.Intent`.
- Produces: `turn_events` unchanged in signature; emits the same `EV_STAGE` / `EV_TOKEN` / `EV_SOURCES` / `EV_ACTIONS` events. No new event names — the nudge is `EV_TOKEN` text.
- Also produces `NUDGE_TEMPLATE: str` and `_nudge(command: str, message: str) -> str` for the test to assert against.

- [ ] **Step 1: Write the failing test**

Create `tests/services/chat/test_engine.py`:

```python
"""One test per routing branch.

`turn_events` is deliberately free of database access, so every branch here is
reachable with fakes and no I/O — which is the whole reason the routing logic
lives in this module rather than in the API layer.
"""

import pytest

from services.chat import engine
from services.chat.intent import INTENT_COMMAND, INTENT_QUESTION, INTENT_SMALLTALK, Intent


class FakeRetriever:
    def __init__(self, excerpts=None):
        self.excerpts = excerpts or []
        self.calls = 0

    async def retrieve(self, user_id, message, history):
        self.calls += 1
        return self.excerpts


def exploding_parser(*args, **kwargs):
    raise AssertionError("parse_command must not be called on this path")


async def collect(**overrides):
    """Drive one turn to completion and return the events as a list."""
    kwargs = {
        "user_id": "u1",
        "message": "hello",
        "history": [],
        "timezone": "UTC",
        "retriever": FakeRetriever(),
        "gmail_connected": True,
    }
    kwargs.update(overrides)
    return [ev async for ev in engine.turn_events(**kwargs)]


def texts(events):
    return "".join(d["text"] for name, d in events if name == engine.EV_TOKEN)


def actions(events):
    for name, data in events:
        if name == engine.EV_ACTIONS:
            return data
    return None


@pytest.fixture
def no_model(monkeypatch):
    """Fail loudly if any model call is made. Individual tests opt back in."""
    monkeypatch.setattr(engine, "parse_command", exploding_parser)

    def classify(*args, **kwargs):
        raise AssertionError("classify must not be called on this path")

    monkeypatch.setattr(engine, "classify", classify)


@pytest.fixture
def answering(monkeypatch):
    """Replace the two streaming answer paths with fixed text."""

    async def answer(message, history, excerpts):
        yield "ANSWER"

    async def smalltalk(message, history):
        yield "SMALLTALK"

    monkeypatch.setattr(engine, "stream_answer", answer)
    monkeypatch.setattr(engine, "stream_smalltalk", smalltalk)


# --- slash branch ------------------------------------------------------


async def test_help_renders_the_registry_without_a_model_call(no_model):
    events = await collect(message="/help")
    assert "`/rule`" in texts(events)
    assert actions(events) is None


async def test_bare_slash_renders_help(no_model):
    assert "`/catchup`" in texts(await collect(message="/"))


async def test_unknown_command_names_it_and_shows_help(no_model):
    body = texts(await collect(message="/sdfsd whatever"))
    assert "/sdfsd" in body
    assert "`/rule`" in body


async def test_fixed_action_command_needs_no_model_call(no_model):
    events = await collect(message="/catchup")
    assert actions(events)["raw"] == [{"type": "catch_up_now"}]


async def test_fixed_action_ignores_trailing_text(no_model):
    events = await collect(message="/briefing about last week")
    assert actions(events)["raw"] == [{"type": "send_briefing_now"}]


async def test_command_needing_args_with_none_gives_usage_and_no_model_call(no_model):
    body = texts(await collect(message="/rule"))
    assert "/rule archive everything from newsletters@x.com" in body
    assert actions(await collect(message="/rule")) is None


async def test_command_with_args_proposes_the_parsed_actions(monkeypatch):
    captured = {}

    def parse(subject, body, tz, allowed_types=None):
        captured["body"] = body
        captured["allowed_types"] = allowed_types
        return {
            "actions": [{"type": "create_rule", "archive": True}],
            "summary": "archive newsletters",
        }

    monkeypatch.setattr(engine, "parse_command", parse)
    events = await collect(message="/rule archive everything from news@x.com")

    assert captured["body"] == "archive everything from news@x.com"
    assert captured["allowed_types"] == ("create_rule",)
    assert actions(events)["raw"] == [{"type": "create_rule", "archive": True}]
    assert "Archive newsletters" in texts(events)


async def test_failed_parse_does_not_fall_through_to_answering(monkeypatch, answering):
    monkeypatch.setattr(
        engine, "parse_command", lambda *a, **k: {"actions": [], "summary": ""}
    )
    retriever = FakeRetriever()
    events = await collect(message="/rule something incoherent", retriever=retriever)

    assert "ANSWER" not in texts(events)
    assert retriever.calls == 0
    assert "/rule archive everything from newsletters@x.com" in texts(events)


async def test_slash_never_reaches_the_classifier(no_model, monkeypatch):
    # `no_model` already explodes on classify; this asserts the whole set.
    for message in ("/help", "/", "/nope", "/catchup", "/rule"):
        await collect(message=message)


# --- prose branch ------------------------------------------------------


async def test_smalltalk_answers_from_the_persona(monkeypatch, answering):
    monkeypatch.setattr(engine, "classify", lambda *a: Intent(INTENT_SMALLTALK))
    monkeypatch.setattr(engine, "parse_command", exploding_parser)
    events = await collect(message="who are you?")
    assert texts(events) == "SMALLTALK"


async def test_question_answers_and_proposes_nothing(monkeypatch, answering):
    monkeypatch.setattr(engine, "classify", lambda *a: Intent(INTENT_QUESTION))
    monkeypatch.setattr(engine, "parse_command", exploding_parser)
    events = await collect(message="what did I miss?")
    assert texts(events) == "ANSWER"
    assert actions(events) is None


async def test_prose_command_answers_then_nudges(monkeypatch, answering):
    monkeypatch.setattr(engine, "classify", lambda *a: Intent(INTENT_COMMAND, "rule"))
    monkeypatch.setattr(engine, "parse_command", exploding_parser)
    message = "can you archive all the marketing emails"
    body = texts(await collect(message=message))

    assert body.startswith("ANSWER")
    assert f"`/rule {message}`" in body


async def test_prose_command_raises_no_confirm_card(monkeypatch, answering):
    """The whole point: a misfiring classifier can no longer produce a card."""
    monkeypatch.setattr(engine, "classify", lambda *a: Intent(INTENT_COMMAND, "rule"))
    monkeypatch.setattr(engine, "parse_command", exploding_parser)
    events = await collect(message="show me my important emails")
    assert actions(events) is None


async def test_classifier_failure_answers_with_no_nudge(monkeypatch, answering):
    def boom(*args, **kwargs):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(engine, "classify", boom)
    body = texts(await collect(message="what did I miss?"))
    assert body == "ANSWER"


async def test_not_connected_path_emits_no_nudge(monkeypatch, answering):
    monkeypatch.setattr(engine, "classify", lambda *a: Intent(INTENT_COMMAND, "rule"))
    monkeypatch.setattr(engine, "parse_command", exploding_parser)
    body = texts(await collect(message="archive everything", gmail_connected=False))
    assert body == engine.NOT_CONNECTED_MESSAGE


async def test_slash_still_works_when_gmail_is_not_connected(no_model):
    """Commands are proposals; connection is checked when they execute."""
    events = await collect(message="/catchup", gmail_connected=False)
    assert actions(events)["raw"] == [{"type": "catch_up_now"}]


async def test_nudge_truncates_a_very_long_message(monkeypatch, answering):
    monkeypatch.setattr(engine, "classify", lambda *a: Intent(INTENT_COMMAND, "do"))
    monkeypatch.setattr(engine, "parse_command", exploding_parser)
    body = texts(await collect(message="x" * 500))
    assert "x" * 201 not in body
```

- [ ] **Step 2: Run it to verify it fails**

```bash
uv run pytest tests/services/chat/test_engine.py -v
```

Expected: many failures — `engine` has no `parse_command` accepting `allowed_types`, no slash handling, and `classify` still returns a string.

- [ ] **Step 3: Update the engine imports and docstring**

In `src/services/chat/engine.py`, replace the module docstring's "Three paths" paragraph (lines 8–19) with:

```
Two gates, in order:

- A message that opens with "/" is a command. It is resolved against the
  registry and proposed as actions — no classification, and for the four
  fixed-action commands, no model call at all.
- Anything else is classified. Smalltalk answers from the persona, a question
  retrieves and answers, and a message that *reads* like a command is answered
  too, with the slash form suggested beneath it.

Prose can no longer mutate anything. That is the point: the classifier used to
sit in front of `parse_command`, so a wrong guess produced a confirm card with
nothing worth approving and no answer either. Now a wrong guess costs one
sentence under a real answer.
```

Then extend the imports block (lines 29–39):

```python
from services.chat.describe import describe_actions
from services.chat.intent import (
    INTENT_COMMAND,
    INTENT_QUESTION,
    INTENT_SMALLTALK,
    Intent,
    classify,
)
from services.chat.sources.base import Excerpt, Retriever
from services.commands import slash
from services.commands.ask import ANSWER_RULES
from services.commands.parser import parse_command
from services.commands.registry import help_text
from services.persona import CAPABILITIES
```

- [ ] **Step 4: Add the nudge helpers**

Add below `NOT_CONNECTED_MESSAGE` (after line 61):

```python
# How much of the user's message is echoed back inside the suggested command.
# Long enough to carry a real instruction, short enough that the chip stays
# readable on one or two lines.
_NUDGE_MESSAGE_CAP = 200

NUDGE_TEMPLATE = (
    "\n\nI only make changes from a slash command — I can't act on a plain "
    "message. Run `{prefill}` to do it."
)


def _nudge(command: str, message: str) -> str:
    """The suggestion appended under an answer to a prose command.

    The prefill echoes the user's own words because that is exactly the input
    the constrained parser wants: `/rule <what they said>`.
    """
    prefill = f"/{command} {' '.join(message.split())[:_NUDGE_MESSAGE_CAP]}".rstrip()
    return NUDGE_TEMPLATE.format(prefill=prefill)
```

- [ ] **Step 5: Update `_intent` for the new return type**

Replace `_intent` (lines 150–163):

```python
async def _intent(user_id: str, message: str, history: list[dict]) -> Intent:
    """Classify the message, degrading to the answer path if the router fails.

    A classifier outage must not fail the turn: `stream_answer` will re-raise a
    genuine configuration problem (a missing API key) with a friendlier message.
    """
    try:
        result = await run_in_threadpool(classify, message, history)
    except Exception:
        log.warning("chat.classify_failed", user_id=user_id, exc_info=True)
        return Intent(INTENT_QUESTION)
    log.info("chat.intent", user_id=user_id, intent=result.kind, command=result.command)
    return result
```

- [ ] **Step 6: Add the slash branch and rewrite the command branch**

Replace `turn_events` (lines 174–232) entirely:

```python
async def _command_events(
    *, user_id: str, message: str, timezone: str
) -> AsyncIterator[tuple[str, dict]]:
    """Handle a message that opens with "/". Never retrieves, never answers."""
    resolved = slash.resolve(message)

    if resolved.kind == slash.KIND_HELP:
        yield EV_TOKEN, {"text": help_text()}
        return

    if resolved.kind == slash.KIND_UNKNOWN:
        log.info("chat.slash_unknown", user_id=user_id, name=resolved.raw_name)
        yield EV_TOKEN, {"text": f"I don't know `/{resolved.raw_name}`.\n\n{help_text()}"}
        return

    command = resolved.command

    if command.fixed_action is not None:
        # Nothing to extract — `/catchup` *is* the action. Trailing text is
        # ignored rather than parsed, so the card always says what will run.
        # (`yield from` is a syntax error in an async generator, hence the loop.)
        for event in _propose([dict(command.fixed_action)], ""):
            yield event
        return

    if not resolved.args:
        yield EV_TOKEN, {"text": f"`/{command.name}` needs a bit more. Try:\n\n`{command.usage}`"}
        return

    yield EV_STAGE, {"label": "Working out what to change"}
    parsed = await run_in_threadpool(
        parse_command, None, resolved.args, timezone, command.action_types
    )
    proposed = parsed.get("actions") or []
    if not proposed:
        # A strict slash rule means saying so. Falling through to an answer
        # would silently swallow a change the user explicitly asked for.
        log.info("chat.slash_no_actions", user_id=user_id, name=command.name)
        yield EV_TOKEN, {
            "text": f"I couldn't work out what to change from that. Try:\n\n`{command.usage}`"
        }
        return

    log.info("chat.actions_proposed", user_id=user_id, name=command.name, count=len(proposed))
    for event in _propose(proposed, parsed.get("summary") or ""):
        yield event


def _propose(proposed: list[dict], summary: str) -> list[tuple[str, dict]]:
    """The lead-in and the confirm card, in that order.

    A card on its own reads as a demand with no explanation, so say what is
    about to happen before asking anyone to approve it.
    """
    return [
        (EV_TOKEN, {"text": _proposal_lead_in(summary)}),
        (
            EV_ACTIONS,
            {"actions": describe_actions(proposed), "raw": proposed, "summary": summary},
        ),
    ]


async def turn_events(
    *,
    user_id: str,
    message: str,
    history: list[dict],
    timezone: str,
    retriever: Retriever,
    gmail_connected: bool,
) -> AsyncIterator[tuple[str, dict]]:
    """Drive one turn, yielding (event_name, payload) pairs."""
    if slash.resolve(message).kind != slash.KIND_NONE:
        async for event in _command_events(
            user_id=user_id, message=message, timezone=timezone
        ):
            yield event
        return

    yield EV_STAGE, {"label": "Reading your question"}

    intent = await _intent(user_id, message, history)

    if intent.kind == INTENT_SMALLTALK:
        async for delta in stream_smalltalk(message, history):
            yield EV_TOKEN, {"text": delta}
        return

    if not gmail_connected:
        # A user with no mailbox connected has a more immediate problem than
        # command syntax, so no nudge here even for INTENT_COMMAND.
        yield EV_SOURCES, {"sources": []}
        yield EV_TOKEN, {"text": NOT_CONNECTED_MESSAGE}
        return

    yield EV_STAGE, {"label": "Searching your mail"}
    try:
        excerpts = await retriever.retrieve(user_id, message, history)
    except Exception:
        # A dead source should degrade the answer, not fail the turn.
        log.warning("chat.retrieve_failed", user_id=user_id, exc_info=True)
        excerpts = []

    yield EV_SOURCES, {"sources": [e.as_dict() for e in excerpts]}
    noun = "email" if len(excerpts) == 1 else "emails"
    yield EV_STAGE, {"label": f"Found {len(excerpts)} {noun}"}

    yield EV_STAGE, {"label": "Writing answer"}
    async for delta in stream_answer(message, history, excerpts):
        yield EV_TOKEN, {"text": delta}

    if intent.kind == INTENT_COMMAND and intent.command:
        log.info("chat.nudged", user_id=user_id, command=intent.command)
        yield EV_TOKEN, {"text": _nudge(intent.command, message)}
```

- [ ] **Step 7: Run the tests to verify they pass**

```bash
uv run pytest tests/services/chat/test_engine.py -v
```

Expected: 17 passed.

- [ ] **Step 8: Run the whole backend suite**

```bash
uv run pytest -v
```

Expected: all passed (48 tests across the four files).

- [ ] **Step 9: Lint and commit**

```bash
uv run ruff check src/services/chat/engine.py tests
uv run mypy src/services/chat src/services/commands
git add src/services/chat/engine.py tests/services/chat/test_engine.py
git commit -m "feat: route slash commands, never mutate from prose

turn_events resolves a leading slash before classifying anything, and the
INTENT_COMMAND branch no longer calls parse_command — so no classifier
misfire can produce a confirm card. Prose that reads as a command is
answered, then the slash form is suggested beneath it."
```

---

## Task 6: Serve the command surface

**Files:**
- Modify: `src/schemas/chat.py` (append `CommandRead`)
- Modify: `src/api/v1/chat.py` (import `registry`; add the route after `list_conversations`, around line 76)

**Interfaces:**
- Consumes: `registry.COMMANDS`.
- Produces: `GET /api/v1/chat/commands` → `[{"name": str, "summary": str, "usage": str}]`. Task 7 consumes this shape.

- [ ] **Step 1: Add the schema**

In `src/schemas/chat.py`, append:

```python
class CommandRead(BaseModel):
    """One slash command, as the web autocomplete menu needs it.

    Served rather than duplicated in TypeScript: an eleven-row list with
    descriptions kept in two repos drifts, and the menu is the only place
    users discover these.
    """

    name: str
    summary: str
    usage: str
```

- [ ] **Step 2: Add the route**

In `src/api/v1/chat.py`, add `CommandRead` to the `schemas.chat` import block (lines 35–41) and add this import beside the other service imports:

```python
from services.commands import registry
```

Then add the route immediately after `list_conversations` (after line 76):

```python
@router.get("/commands", response_model=list[CommandRead])
async def list_commands(user: CurrentUser):
    """The slash command surface, for the web autocomplete menu.

    `CurrentUser` rather than `EntitledUser`: this is a read, and it matches
    how `/conversations*` stay open to a locked account. Knowing what the
    commands are is not the thing worth gating — running them is, and
    `services.commands.handlers.execute` already gates that individually.
    """
    return [
        CommandRead(name=c.name, summary=c.summary, usage=c.usage) for c in registry.COMMANDS
    ]
```

- [ ] **Step 3: Verify the route is registered and ordered correctly**

`/commands` must be declared before any `/{conversation_id}`-style catch-all on the same prefix. It is — `get_conversation` is `/conversations/{id}`, a different path — but confirm the app builds and the route exists:

```bash
uv run python -c "
import sys; sys.path.insert(0, 'src')
from main import app
paths = sorted(r.path for r in app.routes if 'chat' in r.path)
print('\n'.join(paths))
assert any(p.endswith('/chat/commands') for p in paths)
print('OK')
"
```

Expected: the route list including `/api/v1/chat/commands`, then `OK`. If `main` fails to import for want of environment variables, instead run `uv run pytest -q` and verify the app boots under `make up`.

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff check src/api/v1/chat.py src/schemas/chat.py
git add src/api/v1/chat.py src/schemas/chat.py
git commit -m "feat: serve the slash command surface at GET /chat/commands

The web menu reads the registry rather than duplicating it, so the two
repos cannot drift on command names or copy."
```

---

## Task 7: Frontend — fetch the command list

**Files:**
- Modify: `/Users/abcom/Desktop/openfoundry/inboxos-web/src/lib/chat.ts`

**Interfaces:**
- Consumes: `GET /chat/commands` from Task 6.
- Produces:
  - `export type SlashCommandInfo = { name: string; summary: string; usage: string }`
  - `export const listCommands: () => Promise<SlashCommandInfo[]>`

All remaining tasks run in `/Users/abcom/Desktop/openfoundry/inboxos-web`.

- [ ] **Step 1: Add the type and fetcher**

In `src/lib/chat.ts`, after the `Conversation` / `ConversationDetail` types (around line 36), add:

```ts
/** One slash command, as served by GET /chat/commands. */
export type SlashCommandInfo = { name: string; summary: string; usage: string };
```

And beside the other fetchers (after `listConversations`, line 38):

```ts
export const listCommands = () => apiFetch<SlashCommandInfo[]>("/chat/commands");
```

- [ ] **Step 2: Typecheck**

```bash
cd /Users/abcom/Desktop/openfoundry/inboxos-web
npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add src/lib/chat.ts
git commit -m "feat: fetch the slash command surface from the API"
```

---

## Task 8: Frontend — the slash menu

**Files:**
- Create: `src/components/chat/SlashMenu.tsx`
- Modify: `src/components/app/AskBar.tsx`

**Interfaces:**
- Consumes: `SlashCommandInfo` from Task 7.
- Produces:
  - `SlashMenu` props: `{ commands: SlashCommandInfo[]; activeIndex: number; onPick: (name: string) => void }`
  - `AskBar` gains optional props `commands?: SlashCommandInfo[]`, `value?: string`, `onValueChange?: (v: string) => void`. Task 10 passes all three.

- [ ] **Step 1: Write the menu component**

Create `src/components/chat/SlashMenu.tsx`:

```tsx
"use client";

import type { SlashCommandInfo } from "@/lib/chat";

/**
 * The command list that opens above the input when a message starts with "/".
 *
 * Purely presentational — filtering and keyboard state live in AskBar, which
 * owns the input those keys are travelling through.
 */
export default function SlashMenu({
  commands,
  activeIndex,
  onPick,
}: {
  commands: SlashCommandInfo[];
  activeIndex: number;
  onPick: (name: string) => void;
}) {
  if (commands.length === 0) return null;

  return (
    <div
      role="listbox"
      aria-label="Commands"
      className="absolute bottom-full left-0 right-0 z-10 mb-2 max-h-72 overflow-y-auto rounded-2xl border border-ink/10 bg-card py-1.5 shadow-lg"
    >
      {commands.map((c, i) => (
        <button
          key={c.name}
          type="button"
          role="option"
          aria-selected={i === activeIndex}
          // The input must keep focus; mousedown fires before blur.
          onMouseDown={(e) => {
            e.preventDefault();
            onPick(c.name);
          }}
          className={`flex w-full items-baseline gap-3 px-4 py-2 text-left ${
            i === activeIndex ? "bg-ink/5" : ""
          }`}
        >
          <span className="font-mono text-sm font-medium text-ink">/{c.name}</span>
          <span className="flex-1 truncate text-xs text-ink/50">{c.summary}</span>
        </button>
      ))}
    </div>
  );
}
```

- [ ] **Step 2: Rewrite AskBar**

Replace `src/components/app/AskBar.tsx` entirely:

```tsx
"use client";

import { useMemo, useRef, useState } from "react";
import SlashMenu from "@/components/chat/SlashMenu";
import type { SlashCommandInfo } from "@/lib/chat";
import { MicIcon, SendIcon } from "./icons";

const CHIPS = ["Show me my important emails", "What action items do I have?", "What's next for me?"];

/** A message opens a command only while it is still one unbroken word. */
const NAME_FRAGMENT = /^\/([a-z-]*)$/i;

export default function AskBar({
  onSubmit,
  placeholder = "Ask me anything about your meetings or emails…",
  disabled = false,
  busy = false,
  showChips = true,
  commands,
  value: controlledValue,
  onValueChange,
}: {
  onSubmit?: (text: string) => void;
  placeholder?: string;
  disabled?: boolean;
  // Waiting on an answer, as opposed to merely disabled: the button says so.
  busy?: boolean;
  showChips?: boolean;
  // Supplying commands turns on the slash menu. The dashboard's AskBar
  // discards its text and routes to the chat page, so it must not advertise
  // commands it will not run — it simply omits this prop.
  commands?: SlashCommandInfo[];
  // Optional controlled value, so prefill chips elsewhere can write into it.
  value?: string;
  onValueChange?: (v: string) => void;
}) {
  const [uncontrolled, setUncontrolled] = useState("");
  const value = controlledValue ?? uncontrolled;
  const [active, setActive] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function setValue(next: string) {
    if (onValueChange) onValueChange(next);
    else setUncontrolled(next);
  }

  // Only while the whole value is a bare "/name" fragment: once a space is
  // typed the user is into arguments and the menu would sit on top of them.
  const matches = useMemo(() => {
    if (!commands || dismissed) return [];
    const m = NAME_FRAGMENT.exec(value.trimStart());
    if (!m) return [];
    const prefix = m[1].toLowerCase();
    return commands.filter((c) => c.name.startsWith(prefix));
  }, [commands, value, dismissed]);

  const open = matches.length > 0;
  const activeIndex = Math.min(active, matches.length - 1);

  function change(next: string) {
    setValue(next);
    setDismissed(false);
    setActive(0);
  }

  function complete(name: string) {
    setValue(`/${name} `);
    setDismissed(true);
    setActive(0);
    inputRef.current?.focus();
  }

  function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    setValue("");
    setDismissed(false);
    onSubmit?.(trimmed);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!open) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => (i + 1) % matches.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => (i - 1 + matches.length) % matches.length);
    } else if (e.key === "Tab" || e.key === "Enter") {
      // Completing, not sending: Enter submits only once the menu is closed.
      e.preventDefault();
      complete(matches[activeIndex].name);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setDismissed(true);
    }
  }

  return (
    <div className="w-full">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (open) return;
          send(value);
        }}
        className="relative flex items-center gap-3 rounded-full border border-ink/15 bg-card px-5 py-4 shadow-sm"
      >
        {open ? (
          <SlashMenu commands={matches} activeIndex={activeIndex} onPick={complete} />
        ) : null}

        <input
          ref={inputRef}
          value={value}
          onChange={(e) => change(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
          disabled={disabled}
          autoComplete="off"
          aria-expanded={open}
          className="flex-1 bg-transparent text-sm text-ink placeholder:text-ink/40 focus:outline-none disabled:opacity-60"
        />
        <MicIcon className="h-5 w-5 text-ink/20" />
        <button
          type="submit"
          aria-label={busy ? "Waiting for an answer" : "Send"}
          disabled={disabled || busy || !value.trim()}
          // The input is empty right after sending, so the usual disabled
          // dimming would fade the spinner to near-invisible.
          className={`rounded-full bg-accent p-2 text-white hover:bg-accent-dark ${
            busy ? "" : "disabled:opacity-30"
          }`}
        >
          {busy ? (
            <span className="block h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white" />
          ) : (
            <SendIcon className="h-4 w-4" />
          )}
        </button>
      </form>

      {showChips ? (
        <div className="mt-3 flex flex-wrap justify-center gap-2">
          {CHIPS.map((chip) => (
            <button
              key={chip}
              type="button"
              disabled={disabled}
              onClick={() => send(chip)}
              className="rounded-full border border-ink/10 bg-card px-3 py-1.5 text-xs text-ink/60 hover:text-ink disabled:opacity-40"
            >
              {chip}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 3: Typecheck and lint**

```bash
npx tsc --noEmit && npm run lint
```

Expected: no errors. The dashboard's `<AskBar onSubmit={...} />` still typechecks because every new prop is optional.

- [ ] **Step 4: Commit**

```bash
git add src/components/chat/SlashMenu.tsx src/components/app/AskBar.tsx
git commit -m "feat: slash command autocomplete in AskBar

The menu opens only while the value is a bare /name fragment, and only
when a commands list is supplied — the dashboard AskBar discards its text
so it deliberately omits the prop."
```

---

## Task 9: Frontend — prefill chips in answers

**Files:**
- Create: `src/components/chat/PrefillContext.tsx`
- Modify: `src/components/chat/Markdown.tsx` (the `INLINE` regex at line 16 and the `inline` function at 38–66)

**Interfaces:**
- Consumes: `SlashCommandInfo` from Task 7.
- Produces:
  - `PrefillProvider` props: `{ commands: SlashCommandInfo[]; onPrefill: (text: string) => void; children: React.ReactNode }`
  - `usePrefill(): { commands: SlashCommandInfo[]; onPrefill: ((text: string) => void) | null }`

**Why this exists:** `Markdown` renders three levels below `ChatPage`, and the nudge is plain text inside `message.content`. Rather than a new column and a new SSE event, the engine emits the suggestion as a code span and this turns it into a button.

- [ ] **Step 1: Write the context**

Create `src/components/chat/PrefillContext.tsx`:

```tsx
"use client";

import { createContext, useContext } from "react";
import type { SlashCommandInfo } from "@/lib/chat";

type Prefill = {
  commands: SlashCommandInfo[];
  onPrefill: ((text: string) => void) | null;
};

const Ctx = createContext<Prefill>({ commands: [], onPrefill: null });

/**
 * Carries the command list and the input's setter down to Markdown, which
 * renders three levels below the page and would otherwise need both drilled
 * through MessageList and MessageBubble.
 */
export function PrefillProvider({
  commands,
  onPrefill,
  children,
}: Prefill & { children: React.ReactNode }) {
  return <Ctx.Provider value={{ commands, onPrefill }}>{children}</Ctx.Provider>;
}

export const usePrefill = () => useContext(Ctx);
```

- [ ] **Step 2: Add code spans to the Markdown renderer**

`Markdown.tsx` has no inline-code support at all today, so this adds it. In `src/components/chat/Markdown.tsx`:

Add the import at the top, beside the `EmailRefList` import:

```tsx
import { usePrefill } from "./PrefillContext";
```

Replace the `INLINE` constant (line 16):

```tsx
const INLINE = /(\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\)|`[^`]+`)/g;

/** A code span that is exactly a command name, e.g. `/rule` or `/rule do a thing`. */
const COMMAND_SPAN = /^\/([a-z-]+)(?:\s|$)/i;
```

Then replace the `inline` function (lines 38–66) with a version that handles code spans. It becomes a component so it can read the context:

```tsx
function CodeSpan({ text }: { text: string }) {
  const { commands, onPrefill } = usePrefill();
  const name = COMMAND_SPAN.exec(text)?.[1]?.toLowerCase();
  const known = name ? commands.some((c) => c.name === name) : false;

  // Checking against the fetched list is what keeps a path like `/etc/hosts`
  // from rendering as a command chip.
  if (!known || !onPrefill) {
    return (
      <code className="rounded bg-ink/5 px-1.5 py-0.5 font-mono text-[0.85em] text-ink">
        {text}
      </code>
    );
  }

  return (
    <button
      type="button"
      onClick={() => onPrefill(text)}
      className="rounded-md bg-accent/10 px-1.5 py-0.5 font-mono text-[0.85em] font-medium text-accent hover:bg-accent/20"
    >
      {text}
    </button>
  );
}

function inline(text: string, keyPrefix: string) {
  return text.split(INLINE).map((part, i) => {
    const key = `${keyPrefix}-${i}`;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return (
        <strong key={key} className="font-semibold">
          {part.slice(2, -2)}
        </strong>
      );
    }
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      return <CodeSpan key={key} text={part.slice(1, -1)} />;
    }
    const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(part);
    if (link) {
      const href = safeHref(link[2]);
      if (!href) return <span key={key}>{link[1]}</span>;
      return (
        <a
          key={key}
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          className="text-accent underline decoration-accent/30 hover:decoration-accent"
        >
          {link[1]}
        </a>
      );
    }
    return <span key={key}>{part}</span>;
  });
}
```

Also update the module docstring's first paragraph (lines 2–3) to mention code spans:

```
 * A deliberately small markdown renderer for exactly what the assistant emits:
 * **bold**, `code`, `- ` bullets, [label](url), and blank-line paragraphs.
 *
 * A code span naming a slash command renders as a button that prefills the
 * input — which is how the nudge under a prose command becomes actionable
 * without a new event, a new column, or a change to the SSE protocol.
```

- [ ] **Step 3: Typecheck and lint**

```bash
npx tsc --noEmit && npm run lint
```

Expected: no errors. `Markdown` used outside a provider gets the default context (`onPrefill: null`) and renders a plain `<code>`, so nothing breaks before Task 10 wires it up.

- [ ] **Step 4: Commit**

```bash
git add src/components/chat/PrefillContext.tsx src/components/chat/Markdown.tsx
git commit -m "feat: render command code spans as prefill chips

The nudge rides in the message content as ordinary markdown, so it needs
no new SSE event and no new column. Chips are gated on the fetched command
list so a path like /etc/hosts stays plain code."
```

---

## Task 10: Frontend — wire the chat page

**Files:**
- Modify: `src/app/dashboard/chat/page.tsx`

**Interfaces:**
- Consumes: `listCommands` (Task 7), `AskBar`'s `commands` / `value` / `onValueChange` props (Task 8), `PrefillProvider` (Task 9).
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Add the imports**

In `src/app/dashboard/chat/page.tsx`, extend the imports:

```tsx
import { PrefillProvider } from "@/components/chat/PrefillContext";
```

and add `listCommands` and `SlashCommandInfo` to the existing `@/lib/chat` import block:

```tsx
import {
  getConversation,
  listCommands,
  listConversations,
  streamAsk,
  type ActionStatus,
  type ChatMessage,
  type ChatSource,
  type Conversation,
  type SlashCommandInfo,
} from "@/lib/chat";
```

- [ ] **Step 2: Add the draft and command state**

After the `messages` state declaration (line 26), add:

```tsx
const [draft, setDraft] = useState("");
const [commands, setCommands] = useState<SlashCommandInfo[]>([]);
```

After the existing `refreshConversations` effect (line 48), add:

```tsx
// Fetched once: the surface only changes when the server ships a new one.
useEffect(() => {
  if (!configured) return;
  void (async () => {
    try {
      setCommands(await listCommands());
    } catch {
      // No menu is a degraded input, not a broken one — typed commands
      // still work, so this is not worth an error banner.
    }
  })();
}, [configured]);
```

- [ ] **Step 3: Clear the draft on send and on conversation switch**

In `ask`, immediately after `lastQuestion.current = text;` (line 101), add:

```tsx
setDraft("");
```

In `openConversation`, after `setError(null);` (line 59), and in `startNew`, after `setError(null);` (line 77), add to each:

```tsx
setDraft("");
```

- [ ] **Step 4: Wrap the tree and pass the props**

Replace the `return (...)` block's `<main>` contents so both `AskBar`s are controlled and inside the provider. The full replacement for lines 222–277:

```tsx
  return (
    <PrefillProvider commands={commands} onPrefill={setDraft}>
      <div className="flex h-screen">
        <ConversationList
          conversations={conversations}
          activeId={activeId}
          onSelect={(id) => void openConversation(id)}
          onNew={startNew}
          onDeleted={handleDeleted}
        />

        <main className="flex flex-1 flex-col overflow-hidden">
          {empty ? (
            <div className="flex flex-1 flex-col items-center justify-center p-8">
              <span className="mb-8 text-3xl font-semibold tracking-tight text-accent">
                InboxOS
              </span>
              <div className="w-full max-w-2xl">
                <AskBar
                  onSubmit={(t) => void ask(t)}
                  disabled={streaming}
                  busy={streaming}
                  placeholder="Ask me anything about your emails…"
                  commands={commands}
                  value={draft}
                  onValueChange={setDraft}
                />
                <p className="mt-3 text-center text-xs text-ink/40">
                  Type <span className="font-mono text-ink/60">/</span> for commands
                </p>
                {error ? <p className="mt-4 text-center text-sm text-accent">{error}</p> : null}
              </div>
            </div>
          ) : (
            <>
              <div className="flex-1 overflow-y-auto">
                <MessageList
                  messages={messages}
                  streaming={streaming}
                  stage={stage}
                  streamedText={streamedText}
                  streamedSources={streamedSources}
                  error={error}
                  onRetry={() => void ask(lastQuestion.current)}
                  onActionsResolved={resolveActions}
                />
              </div>
              <div className="border-t border-black/5 bg-canvas px-4 py-4">
                <div className="mx-auto w-full max-w-3xl">
                  <AskBar
                    onSubmit={(t) => void ask(t)}
                    disabled={streaming}
                    busy={streaming}
                    showChips={false}
                    placeholder="Ask a follow-up, or / for commands…"
                    commands={commands}
                    value={draft}
                    onValueChange={setDraft}
                  />
                </div>
              </div>
            </>
          )}
        </main>
      </div>
    </PrefillProvider>
  );
```

- [ ] **Step 5: Typecheck and lint**

```bash
npx tsc --noEmit && npm run lint
```

Expected: no errors.

- [ ] **Step 6: Manual verification**

Start the backend (`make up` in `InboxPilot`) and the web app (`npm run dev`), sign in, open `/dashboard/chat`, and confirm each:

1. Typing `/` opens the menu with all eleven commands.
2. Typing `/r` filters to `/remind`, `/rule`, `/routine`.
3. `↓` then `Enter` completes to `/rule ` and does **not** send.
4. `Esc` closes the menu; `Enter` then sends.
5. `/help` returns the command list, with each name rendered as a clickable chip.
6. Clicking a chip puts that command in the input.
7. `/catchup` returns a confirm card immediately, with no "Working out what to change" stage.
8. `/sdfsd` returns "I don't know `/sdfsd`" plus the list.
9. `can you archive all the marketing emails` returns a **normal answer** followed by a clickable `/rule …` chip, and **no confirm card**.
10. The dashboard's AskBar (`/dashboard`) does **not** open a menu on `/`.

- [ ] **Step 7: Commit**

```bash
git add src/app/dashboard/chat/page.tsx
git commit -m "feat: wire the slash menu and prefill chips into the chat page"
```

---

## Task 11: Frontend — restyle the approval card

**Files:**
- Modify: `src/components/app/icons.tsx` (append two icons)
- Modify: `src/components/chat/ActionConfirm.tsx`

**Interfaces:**
- Consumes: `ChatAction` (`{ type, label, detail }`) — unchanged from today.
- Produces: nothing consumed by later tasks.

**Scope note:** the reference screenshot's title reads "Archive 3 threads". That count needs the affected-email preview the design explicitly cut (spec §2), so a `create_rule` card keeps its count-free label. Do not add a Gmail call here.

- [ ] **Step 1: Add the icons**

Append to `src/components/app/icons.tsx`, following the existing `IconProps` pattern used by every other icon in the file:

```tsx
export function WarnIcon(p: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} {...p}>
      <path d="M12 4 2.5 20h19L12 4Z" strokeLinejoin="round" />
      <path d="M12 10v4" strokeLinecap="round" />
      <path d="M12 17.5v.01" strokeLinecap="round" />
    </svg>
  );
}

export function XIcon(p: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} {...p}>
      <path d="M6 6l12 12M18 6 6 18" strokeLinecap="round" />
    </svg>
  );
}
```

Check the top of the file first — if the existing icons spread `{...p}` differently (e.g. a shared `className` default), match whatever they do rather than the above.

- [ ] **Step 2: Restyle the card**

Replace the `return (...)` block of `src/components/chat/ActionConfirm.tsx` (lines 36–89), and extend the import at line 4 to `import { CheckIcon, WarnIcon, XIcon } from "@/components/app/icons";`:

```tsx
  // A single action names itself; several are counted, because listing the
  // first one in the title would misrepresent what Approve actually does.
  const title =
    actions.length === 1
      ? actions[0].label
      : `${actions.length} changes`;

  return (
    <div className="overflow-hidden rounded-2xl border border-ink/10 bg-card">
      <div className="p-4">
        <div className="mb-3 flex items-center gap-2">
          <WarnIcon className="h-5 w-5 shrink-0 text-ink" />
          <p className="text-sm font-semibold text-ink">{title}</p>
        </div>

        <ul className="space-y-2">
          {actions.map((a, i) => (
            <li key={i} className="text-sm">
              {actions.length > 1 ? (
                <span className="font-medium text-ink">{a.label}</span>
              ) : null}
              {a.detail ? <span className="block text-xs text-ink/50">{a.detail}</span> : null}
            </li>
          ))}
        </ul>

        {status === "rejected" ? (
          <p className="mt-3 text-xs text-ink/40">Denied — nothing was changed.</p>
        ) : null}

        {status === "confirmed" ? (
          <ul className="mt-3 space-y-1">
            {results.map((r, i) => (
              <li key={i} className="flex items-start gap-1.5 text-xs text-ink/60">
                <CheckIcon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" />
                {r}
              </li>
            ))}
          </ul>
        ) : null}

        {error ? <p className="mt-3 text-xs text-accent">{error}</p> : null}
      </div>

      {status === "pending" ? (
        <div className="flex items-center gap-2 border-t border-ink/10 px-4 py-3">
          <button
            type="button"
            disabled={busy}
            onClick={() => decide(false)}
            className="flex items-center gap-1.5 rounded-xl px-3 py-2 text-sm font-medium text-ink/70 hover:text-ink disabled:opacity-50"
          >
            <XIcon className="h-4 w-4" />
            Deny
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => decide(true)}
            className="flex items-center gap-1.5 rounded-xl bg-accent px-4 py-2 text-sm font-semibold text-white hover:bg-accent-dark disabled:opacity-50"
          >
            <CheckIcon className="h-4 w-4" />
            {busy ? "Working…" : "Approve"}
          </button>
        </div>
      ) : null}
    </div>
  );
```

- [ ] **Step 3: Typecheck and lint**

```bash
npx tsc --noEmit && npm run lint
```

Expected: no errors.

- [ ] **Step 4: Manual verification**

With both apps running:

1. `/catchup` → card titled "Send a catch-up summary now", Deny on the left, Approve accent-filled on the right.
2. `/vip add stripe.com and pradeep@acme.com` → card titled "Add to your VIP list" with the detail line beneath.
3. Approve → the card shows the check-marked result lines and the footer disappears.
4. Deny on a fresh card → "Denied — nothing was changed."
5. Reload the conversation → the resolved card renders the same way from the persisted transcript.

- [ ] **Step 5: Commit**

```bash
git add src/components/app/icons.tsx src/components/chat/ActionConfirm.tsx
git commit -m "feat: restyle the approval card

Warning title, Deny/Approve footer split off by a rule. No affected-email
preview — that needs a propose-time Gmail query, which the design cut."
```

---

## Final verification

- [ ] **Backend suite**

```bash
cd /Users/abcom/Desktop/openfoundry/InboxPilot
uv run pytest -v
uv run ruff check src tests
uv run mypy src
```

Expected: all tests pass, lint clean.

- [ ] **No migration was created**

```bash
git log --oneline --name-only -12 | grep alembic || echo "OK — no migration"
```

Expected: `OK — no migration`.

- [ ] **The email surface still parses commands with every action type**

```bash
grep -n "parse_command" src/workers/jobs/handle_command_email.py
```

Expected: the call is unchanged and passes no `allowed_types`, so it composes the full prompt.

- [ ] **Frontend builds**

```bash
cd /Users/abcom/Desktop/openfoundry/inboxos-web
npx tsc --noEmit && npm run lint && npm run build
```

Expected: a clean production build.
