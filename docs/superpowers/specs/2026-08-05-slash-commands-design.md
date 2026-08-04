# Slash Commands in Chat — Design

**Date:** 2026-08-05
**Status:** Draft, for engineering review
**Repos:** `InboxPilot` (backend), `inboxos-web` (frontend)

---

## 1. The problem

Chat routes every message through an LLM classifier (`src/services/chat/intent.py`)
that returns `smalltalk` / `question` / `command`. Only `command` reaches
`parse_command`, and only `parse_command` can raise a confirm card.

That classifier is the bug. It has to make a fine-grained judgement — is "show me
my important emails" a request to *see* mail or to *file* it? — and when it guesses
`command` on a plain question, the user gets a confirm card with nothing worth
approving and no answer either. The docstring in `intent.py` already names this as
"the worst possible answer"; the fix attempted there was a longer prompt, and a
longer prompt cannot make an inherently ambiguous call reliable.

The real defect is that a probabilistic classifier sits on the path to a state
change. No amount of prompt work removes that.

## 2. The decision

**A state change requires an explicit slash command.** Prose never mutates
anything.

This does not make the classifier correct — it makes the classifier's mistakes
cheap. Under this design a misfire costs the user one extra sentence of
suggestion appended to a real answer, instead of a dead-end card. The classifier
survives, demoted from *decides whether to act* to *decides whether to suggest*.

Four decisions follow, settled during design:

| Decision | Choice |
|---|---|
| Is slash the only path to a command? | **Yes, strict.** Prose can never mutate. |
| How are arguments parsed? | **Slash pins the action type, the LLM fills the fields.** Natural language is preserved. |
| What is the command surface? | **10 commands grouped by noun**, plus `/do` as an escape hatch and `/help`. |
| What happens to a prose command? | **Answer it, then append a clickable slash suggestion.** |

Out of scope, considered and cut:

- **Previewing the affected emails** before approval (the collapsible email list in
  the reference screenshot). It needs a live Gmail query at propose-time, where
  today the query only runs at approve-time; it adds latency to every proposal and
  the preview can go stale between propose and approve. Deferred.
- **A persisted stage checklist** with per-stage icons. `EV_STAGE` currently
  replaces rather than accumulates. Deferred.
- **Slash commands on the email-to-self surface.** Email is deliberately
  "every message is a candidate command" — that surface has no misclassification
  problem to solve, because a mail you send yourself is usually an instruction.

## 3. Command surface

`/help` lists these. `GET /chat/commands` serves the same data to the web menu.

| Command | Summary | Usage | Action types | Fixed |
|---|---|---|---|---|
| `/catchup` | Summarise important unread mail | `/catchup` | `catch_up_now` | ✓ |
| `/briefing` | Send a briefing right now | `/briefing` | `send_briefing_now` | ✓ |
| `/invoices` | Summarise recent invoices | `/invoices` | `summarize_invoices_now` | ✓ |
| `/deadlines` | Find deadlines and set reminders | `/deadlines` | `scan_deadlines_now` | ✓ |
| `/remind` | Set a reminder | `/remind me tomorrow 3pm to call the bank` | `set_reminder` | |
| `/vip` | Add or remove VIP senders | `/vip add stripe.com` | `add_vip`, `remove_vip` | |
| `/rule` | Create a filing rule | `/rule archive everything from newsletters@x.com` | `create_rule` | |
| `/label` | Create or delete a Gmail label | `/label create Receipts` | `create_label`, `delete_label` | |
| `/routine` | Turn a scheduled routine on or off | `/routine briefing every morning at 8` | `manage_routine` | |
| `/delivery` | Change batching, quiet hours, timing | `/delivery 3 times a day, quiet hours 22:00–07:00` | `set_routine` | |
| `/do` | Anything else | `/do <what you want>` | *all types* | |

Two properties worth stating explicitly:

**Every handler type is reachable.** The ten named commands cover all twelve types
in `services/commands/handlers.execute`, and `/do` is the unconstrained escape
hatch so nothing becomes unreachable under a strict slash rule. A test enforces
both directions of this mapping (§8).

**The four "Fixed" commands never call the model.** `/catchup` *is*
`{"type": "catch_up_now"}` — there is nothing to extract. They emit a confirm card
directly: no latency, no token cost, no possible misparse.

## 4. Backend: the registry

New `src/services/commands/registry.py`. One frozen dataclass, one tuple, and the
lookup helpers — this is the single source of truth for the command surface, the
`/help` text, the intent classifier's prompt, and the web menu.

```python
@dataclass(frozen=True)
class SlashCommand:
    name: str                          # "vip"
    summary: str                       # menu row and /help line
    usage: str                         # "/vip add stripe.com"
    action_types: tuple[str, ...]      # ("add_vip", "remove_vip")
    fixed_action: dict | None = None   # set => no model call

COMMANDS: tuple[SlashCommand, ...] = (...)   # table order from §3
HELP_NAME = "help"

def lookup(name: str) -> SlashCommand | None: ...   # case-insensitive
def help_text() -> str: ...                          # Markdown, /help + unknown-command
```

`help_text()` renders each command as a Markdown code span (`` `/rule` ``) so the
web turns it into a clickable chip for free (§6).

## 5. Backend: constrained parsing

`src/services/commands/parser.py` today holds one ~50-line `_SYSTEM` blob listing
every action shape and every rule. It splits into:

- **`_ACTION_SPECS: dict[str, str]`** — type → its JSON shape *plus its
  type-specific rules*. The `manage_routine` briefing rule, the `create_rule`
  label-vs-category rule, the `set_routine` frequency rules, the `set_reminder`
  relative-time rule each travel with their type.
- **`_GLOBAL_RULES`** — only the type-agnostic ones: omit unused fields, never emit
  `"..."` placeholders, 24h `HH:MM` times, be conservative.
- **`build_system(types: tuple[str, ...] | None) -> str`** — composes a prompt for
  any subset, in registry order so the prefix stays stable for prompt caching.

The public signature gains one optional argument:

```python
def parse_command(subject, body, tz=None, allowed_types=None) -> dict
```

`allowed_types=None` composes all types, so **`workers/jobs/handle_command_email.py`
and the whole email-to-self path are unchanged.** Slash passes a subset.

Returned actions are **also post-filtered** against `allowed_types` — a constrained
prompt still lets the model stray — and anything dropped is logged as
`commands.action_out_of_scope`.

The payoff beyond fixing the routing bug: `/rule` prompts the model with roughly
six lines instead of fifty, so it cannot emit a `set_routine` when you asked for a
filing rule. That confusion is possible today.

## 6. Backend: routing

New `src/services/commands/slash.py` — pure string work, no I/O, no model:

```python
@dataclass(frozen=True)
class Resolution:
    kind: str                       # "none" | "help" | "unknown" | "command"
    command: SlashCommand | None
    args: str
    raw_name: str                   # for the unknown-command message

def resolve(message: str) -> Resolution: ...
```

Rules, each of which gets a test:

- Leading whitespace is stripped first, so `"  /vip add x"` resolves.
- Only a leading `/` counts. `"what about /r/email"` is prose (`kind="none"`).
- Bare `"/"` and `"/help"` both return `kind="help"` — never `"unknown"`.
- The name is matched case-insensitively; **args keep their original case**,
  because they carry email addresses and label names.
- Unknown name returns `kind="unknown"` with `raw_name` for the error message.

`services/chat/engine.turn_events` gains one branch at the top and loses one:

```
message starts with "/" ?
  ├─ help                       → registry.help_text(). No model call.
  ├─ unknown                    → "I don't know /foo." + help_text(). No model call.
  ├─ fixed_action               → lead-in + confirm card. No model call.
  ├─ args empty (no fixed_action) → usage hint. No model call.
  ├─ parse returned actions     → lead-in + confirm card.
  └─ parse returned nothing     → usage hint. Does NOT fall through to answering.
otherwise
  ├─ smalltalk                  → unchanged
  ├─ question                   → unchanged
  └─ command                    → answer path, then append the nudge
```

The last line of the slash branch matters: under a strict rule, a failed slash
parse must say so. Falling through to an answer — which the current
`INTENT_COMMAND` branch does — would silently swallow a command the user
explicitly asked for.

**The deletion is the fix:** `INTENT_COMMAND` no longer calls `parse_command`, so
no classifier misfire can produce a confirm card again.

### The nudge

`intent.py`'s prompt is rewritten around its easier new job and returns the
suggested command in the same call:

```json
{"intent": "command", "command": "rule"}
```

The command names and summaries are injected into that prompt from `registry.py`,
so the classifier cannot suggest a command that does not exist. If it does anyway,
or omits the field, the suggestion falls back to `/do`. **The nudge costs zero
extra model calls.**

The engine appends, after the answer tokens have finished streaming:

> I only make changes from a slash command — I can't act on a plain message. Run
> `` `/rule can you archive all the marketing emails` `` to do it.

The prefill is `/{name} {original message}`, capped at 200 characters. The user's
own words are exactly what the constrained parser wants as input.

The nudge is **skipped** on the not-connected path (`NOT_CONNECTED_MESSAGE`) — a
user with no Gmail connection has a more immediate problem than command syntax.

**No migration.** The nudge is ordinary assistant text, streamed as tokens and
persisted in `chat_messages.content` like any other reply. It survives reload for
free and needs no new column. `Markdown.tsx` turns the code span into a clickable
chip at render time (§7), which also makes `/help` output clickable as a side
effect.

## 7. Frontend: `inboxos-web`

**`GET /chat/commands`** (new, in `api/v1/chat.py`) returns
`[{"name", "summary", "usage"}]` from the registry. Auth is `CurrentUser`, not
`EntitledUser` — it is a read endpoint, matching how `/conversations*` stay open.
A hardcoded TypeScript copy of an 11-row list with descriptions *will* drift from
`registry.py`; serving it makes drift impossible. `src/lib/chat.ts` gains
`listCommands()`, fetched once per chat page and cached in state.

**`src/components/chat/SlashMenu.tsx`** (new) — the popup above the input.

**`src/components/app/AskBar.tsx`** gains three optional props:

- `slashMenu?: boolean` (default `false`). The dashboard's AskBar discards its text
  and routes to `/dashboard/chat`, so it must not advertise commands it will not
  run. Only the two chat-page AskBars pass `true`.
- `value` / `onValueChange` — makes AskBar optionally **controlled** so prefill
  chips can write into it. Undefined leaves it uncontrolled and the dashboard
  untouched.

Menu behaviour:

- Opens when the trimmed value starts with `/` **and contains no space yet**; typing
  a space closes it and the user is into free-text arguments.
- Filters by name prefix. No match shows nothing rather than an empty box.
- `↑`/`↓` move the selection, `Tab` and `Enter` complete to `/name ` **without
  submitting**, `Esc` closes the menu without clearing the input.
- `Enter` submits only when the menu is closed.
- Each row shows name + summary; the active row shows `usage` as a hint line.

**`src/components/chat/PrefillContext.tsx`** (new) — a context carrying
`(text: string) => void` plus the fetched command list. `ChatPage` provides it and
holds the draft value. Context rather than drilling a callback through
`MessageList → MessageBubble → Markdown`.

**`src/components/chat/Markdown.tsx`** — the inline-code renderer checks whether the
span matches `^/[a-z-]+(\s|$)` *and* names a command in the fetched list. If so it
renders a button that prefills the input instead of a `<code>`. The list check is
what keeps a path like `` `/etc/hosts` `` from becoming a chip.

**`src/app/dashboard/chat/page.tsx`** — holds the draft state, provides the context,
and adds a `Type / for commands` line under the empty-state chips.

### Approval card restyle

`src/components/chat/ActionConfirm.tsx`, to match the reference screenshot:

- A warning triangle beside a title, replacing the
  `Confirm to continue` eyebrow. The title is the single action's label, or
  `N changes` when there is more than one.
- Footer separated by a top border; **`Deny`** (renamed from `Dismiss`) on the left
  with an X icon, `Approve` on the right, accent-filled with a check icon.
- Card chrome to match: `rounded-2xl border border-ink/10`.
- New `WarnIcon` and `XIcon` in `src/components/app/icons.tsx`.

One honest limitation: the screenshot's title reads *"Archive 3 threads"*, and we
cannot produce that count without the affected-email preview cut in §2. A
`create_rule` proposal keeps a count-free title such as *"Create a Gmail rule"*,
with the criteria and effects in the detail line as they are today.

## 8. Testing

`pyproject.toml` already configures pytest with `testpaths = ["tests"]`, but no
`tests/` directory exists. This project creates it. `engine.turn_events` is
deliberately free of database access, so every routing case below is reachable
with fakes and no I/O.

**`tests/services/commands/test_slash.py`** — every rule in §6: leading whitespace,
mid-message slash, bare `/`, `/help`, case-insensitive names, args preserving case,
unknown names.

**`tests/services/commands/test_registry.py`** — the drift guard, both directions:
every `action_types` entry is a type `handlers.execute` dispatches on, and every
type `handlers.execute` dispatches on is reachable from at least one command. This
is what catches a thirteenth action type being added without a command.

**`tests/services/commands/test_parser.py`** — `build_system` includes only the
requested types and always the global rules; ordering is stable; out-of-scope
actions returned by the model are filtered out; `allowed_types=None` composes every
type, so the email path is unchanged.

**`tests/services/chat/test_engine.py`** — one test per branch of the §6 table, plus:
a fixed-action command with a parser fake that raises if called (proving no model
call), a prose command yielding answer tokens *followed by* the nudge, a classifier
raising and still producing an answer with no nudge, and the not-connected path
emitting no nudge.

## 9. Migration and rollout

- **No Alembic migration.** No schema change anywhere in this design.
- **No behaviour change on the email surface.** `parse_command`'s default argument
  keeps `handle_command_email.py` byte-for-byte equivalent.
- **Existing chat transcripts** are unaffected: past confirm cards keep their
  `pending` status and `/messages/{id}/confirm` is untouched.
- **The one user-visible regression** is intended: a prose command that used to
  raise a card now raises an answer plus a suggestion. The suggestion chip is the
  entire mitigation, and it is one click.
- **Ship order.** Backend first (`registry` → `parser` → `slash` → `engine` →
  `intent` → `GET /chat/commands`), since `/help` alone makes the feature usable
  from the existing input before any web work lands. Then web.
