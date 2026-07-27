# Web Chat — Ask Your Mail

**Date:** 2026-07-27
**Repos:** `InboxPilot` (FastAPI backend), `inboxos-web` (Next.js web app)
**Branch:** `feat/chat` in both

## Problem

`/dashboard/chat` in the web app is a mock. It has an unwired input, a sidebar
that says "No conversations yet", and suggestion chips that do nothing.

The backend already answers questions about a user's mail — `services/commands/ask.py`
plans several complementary Gmail searches, runs them through Composio, dedupes the
hits, and answers grounded in the real messages. But it is only reachable by emailing
yourself. There is no HTTP endpoint and no conversation storage.

This spec wires the existing engine to the existing UI, adds persistence, and puts a
confirmation step in front of the command handlers.

## Scope

Users can ask questions about their mail in the web app and get grounded answers with
citations. When a message is a *command* rather than a question, chat proposes the
actions and executes only on explicit approval.

Retrieval is structured behind a source seam so meeting notes and Notion can be added
later without touching the engine. Neither is implemented here.

### Out of scope

Voice input; server-side conversation search; renaming conversations;
regenerate/edit-message; reading attachment *contents* (names and counts only, as
today); Notion and meeting-notes retrievers; any behavioural change to the
email-reply path.

## Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Persistence | Postgres tables | Sidebar history and follow-up questions both need server-side turns. Client-only history dies per-device and is invisible to future integrations. |
| Transport | SSE (stages + tokens) | A turn takes 5–15s. Staged progress plus streamed tokens beats a spinner, and costs one `StreamingResponse` plus a small client reader. |
| Command handling | Propose, then confirm | Chat can do the full command set, but a misparsed message must never trash mail unattended. |
| LLM provider | OpenAI, existing code | Reuses `ask.py` / `parser.py` unchanged, so web and email answers stay consistent. Swappable later behind the service boundary. |

## Architecture

```
Browser  ──POST /api/chat/ask──▶  Next rewrite  ──▶  FastAPI  /v1/chat/ask
   ▲                                                     │
   └────────── SSE: stage / token / sources / actions ────┘
                                                          │
                                    services/chat/engine.py
                                      │            │
                     parser.parse_command()   sources/email_source.py
                        (command intent)        │
                                                ask.plan_queries()
                                                ask.search_all()
                                                ask.build_corpus()
                                                AsyncOpenAI(stream=True)
```

One turn, two paths. `parser.parse_command(subject=None, body=message, tz=...)` runs
first and returns `{"actions": [...], "summary": "..."}`. Non-empty `actions` → the
propose-actions path (nothing executes). Empty → the answer path.

Retrieval re-runs on every answer turn, with `recent_turns` history passed to the
planner — so a follow-up like "what about the second one?" resolves against the
prior turn instead of searching for the literal phrase.

## Backend

### Data model — `src/models/chat.py`

```python
class ChatConversation(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "chat_conversations"
    user_id         # FK users.id CASCADE, indexed
    title           # String(200) — first 60 chars of the first user message
    last_message_at # DateTime(tz), indexed — sidebar ordering

class ChatMessage(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "chat_messages"
    conversation_id # FK chat_conversations.id CASCADE, indexed
    role            # String(16): "user" | "assistant"
    content         # Text
    sources         # JSONB [] — [{message_id, thread_id, sender, subject, date, link}]
    actions         # JSONB [] — proposed actions in parser.py shapes, unexecuted
    action_status   # String(16) default "none": none | pending | confirmed | rejected
    action_results  # JSONB [] — handler result lines, after execution
```

Pending actions live on the assistant message row. A confirm card therefore survives
a page reload, and there is no separate pending-action table to reconcile.

One alembic migration creates both tables.

### `src/services/chat/`

- **`store.py`** — conversation and message CRUD, in the style of
  `services/mailman/store.py`. Includes `recent_turns(db, conversation_id, n=6)`,
  which feeds follow-up context to both the planner and the answerer.
- **`sources/base.py`** — the extension seam. A narrow protocol:
  `Retriever.retrieve(user_id, question, history) -> list[Excerpt]`, where `Excerpt`
  is `{kind, title, sender, date, link, text}`. Adding a retriever later means one
  new file plus one registry entry; the engine does not change.
- **`sources/email_source.py`** — the only implementation now. Wraps the Gmail
  plan-and-search from `ask.py`.
- **`engine.py`** — orchestrates one turn as an async generator of events.

### Refactor of existing code

`ask.py`'s `_plan_queries`, `_search_all` and `_corpus` become public
`plan_queries`, `search_all`, `build_corpus`. `answer_question` keeps its exact
behaviour and signature, so the email path is untouched.

The chat engine calls those three functions rather than duplicating the prompts —
retrieval improvements then land on both surfaces at once. The answer call in chat
uses `AsyncOpenAI` with `stream=True`; blocking Composio fetches go through
`run_in_threadpool`.

`openai` is added to `pyproject.toml` dependencies. `ask.py` already imports it, but
it is currently satisfied transitively through `composio` — a dependency bump could
break the app today.

### API — `src/api/v1/chat.py`, registered in `api/router.py`

```
GET    /v1/chat/conversations           → [{id, title, last_message_at}]
GET    /v1/chat/conversations/{id}      → {id, title, messages: [...]}
DELETE /v1/chat/conversations/{id}      → 204
POST   /v1/chat/ask                     → text/event-stream
POST   /v1/chat/messages/{id}/confirm   → {approve: bool} → updated message
```

`POST /v1/chat/ask` takes `{conversation_id?: uuid, message: str}`. Omitting
`conversation_id` creates a conversation.

Every endpoint filters on `user_id` from `get_current_user`, so one user cannot read
or confirm another's messages.

### SSE event protocol

| event | data | when |
| --- | --- | --- |
| `conversation` | `{id, title}` | first, so a new chat can attach immediately |
| `stage` | `{label}` | "Reading your question" → "Searching your mail" → "Found 8 emails" → "Writing answer" |
| `token` | `{text}` | answer deltas |
| `sources` | `{sources: [...]}` | after retrieval |
| `actions` | `{message_id, actions: [{type, label, detail}]}` | command path, instead of tokens |
| `done` | `{message_id}` | last event on success |
| `error` | `{message}` | terminal failure |

Response headers: `Cache-Control: no-cache`, `Connection: keep-alive`,
`X-Accel-Buffering: no`.

The generator opens its **own** `SessionLocal()` rather than using the request-scoped
`DbSession`. The dependency's session closes when the response starts — before the
generator has finished writing the assistant message.

### Confirm endpoint

Loads the message, asserts ownership and `action_status == "pending"`, then runs
`handlers.execute` per action. Sets `action_status` to `confirmed` (or `rejected`
when `approve` is false), stores the result lines, returns the updated message.

## Frontend

### `src/lib/chat.ts`

Types plus thin `apiFetch` wrappers, mirroring `lib/mailman.ts`. One non-standard
piece: `streamAsk()`. `EventSource` cannot POST, so it uses `fetch` with
`res.body.getReader()` and a small SSE line parser, dispatching to callbacks:

```ts
streamAsk(
  { conversationId, message },
  { onConversation, onStage, onToken, onSources, onActions, onDone, onError },
  signal,   // AbortController — cancels an in-flight answer
)
```

### `src/components/chat/`

- **`ConversationList.tsx`** — replaces the fake sidebar. Real conversations ordered
  by `last_message_at`, active highlight, working "New Chat", hover-delete. The
  existing search box becomes a client-side title filter; there is no server search
  endpoint.
- **`MessageList.tsx`** — scrollback, auto-scrolls to bottom as tokens arrive.
- **`MessageBubble.tsx`** — user right-aligned plain text, assistant left-aligned
  markdown.
- **`Markdown.tsx`** — a small renderer for exactly what `_ANSWER_SYS` emits:
  `**bold**`, `- ` bullets, `[label](url)`, paragraphs. No new dependency and no
  `dangerouslySetInnerHTML` — it builds React nodes, so model output cannot inject
  markup. Links are restricted to `http(s):` and get
  `target="_blank" rel="noopener noreferrer"`.
- **`SourceList.tsx`** — collapsed `▸ 8 emails` under an answer; expands to
  sender / subject / date rows linking to the Gmail thread.
- **`ActionConfirm.tsx`** — the confirm card: action labels, Approve / Dismiss. On
  approve, calls the confirm endpoint and swaps in the returned result lines. Renders
  read-only once `action_status != "pending"`, so a reloaded page shows what was
  decided.
- **`StageIndicator.tsx`** — the current stage label with a pulse dot while streaming.

### `src/app/dashboard/chat/page.tsx`

Rewritten to compose the above. Two states: empty (centered "InboxOS" wordmark,
`AskBar`, chips — as today) and active (transcript with the composer pinned to the
bottom).

`AskBar` gains a real `onSubmit(text)`, Enter-to-send, and a `disabled` prop while
streaming; the chips become working prompt fills. `MicIcon` stays decorative — no
speech work in this branch.

The page follows the mailman page's `backendConfigured()` guard: with no
`NEXT_PUBLIC_API_URL` it renders the empty state rather than throwing.

### Streaming through the proxy

SSE has to survive the `next.config.mjs` rewrite. Next 14 streams rewrites, but
buffering is the classic failure mode here. If the proxy buffers in practice, the
fallback is a thin `src/app/api/chat/ask/route.ts` that pipes the upstream body
through — roughly 20 lines, not a redesign. Streaming is verified end-to-end in a
browser before this is called done.

## Error handling

| Failure | Behaviour |
| --- | --- |
| `OPENAI_API_KEY` unset | Single `error` event, "Chat isn't configured yet." UI shows a failed bubble with Retry. |
| Gmail not connected | Engine checks `gmail.is_connected` before retrieval and answers with a message pointing at `/onboarding/connect`. No 500. |
| One Gmail query fails | Already handled: `search_all` logs `ask.query_failed` and skips it. The answer proceeds on what was found. |
| Zero results | `_ANSWER_SYS` already covers it — states what is missing and suggests a next step. `sources` carries `[]`. |
| Mid-stream exception | `error` event, generator closes. The partial assistant message is persisted, so the transcript has no hole. |
| Confirm on a non-pending message | 409 via `ConflictError`. Covers double-clicks and stale tabs. |
| One action fails during confirm | Remaining actions still run; failures get a `"failed: …"` result line, as on the email path. |
| User navigates away mid-answer | `AbortController` cancels the fetch; the server generator still commits the partial message. |

## Testing

`pytest` and `pytest-asyncio` are already configured (`testpaths=["tests"]`,
`pythonpath=["src"]`) but no `tests/` directory exists. These are the first tests in
the repo.

- **`tests/conftest.py`** — a session fixture plus fakes for the two external edges:
  a fake OpenAI (scripted plan JSON and scripted answer chunks) and a fake
  `gmail.fetch_by_query` returning canned `EmailSummary` objects. No network, no
  Composio.
- **`tests/services/chat/test_engine.py`** — a question emits
  `stage`/`sources`/`token`/`done` in order; a command emits `actions` and executes
  nothing; no Gmail connection yields a graceful message; a failing query still
  produces an answer.
- **`tests/services/chat/test_store.py`** — a conversation is created on first ask,
  the title derives from the first message, `last_message_at` advances,
  `recent_turns` returns the last 6 in order.
- **`tests/api/test_chat.py`** — ownership isolation (user B gets 404 on user A's
  conversation) and the confirm state machine (`pending → confirmed`, second call
  409).

`inboxos-web` has no test runner and adding one is its own project. The web side is
verified manually against a running backend, plus clean `npm run build` and
`npm run lint`. Which flows were exercised in a browser is reported explicitly.

## Known limitation (follow-up work)

`handlers.execute` is `async` but makes blocking Composio calls inside it. Confirming
an action stalls the event loop for roughly 1–2s. Making `handlers.py`
threadpool-safe touches the working email command path, so it is deliberately outside
this branch.
