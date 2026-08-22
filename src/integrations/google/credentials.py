"""Reading, refreshing, and storing the user's Google grant.

Everything that touches `google_connections` goes through here, so there is one
answer to "is this user connected", one place that refreshes a token, and one
place that decides a grant is dead.

**Synchronous, and callable from anywhere.** The DB layer is async-only, so the
authoritative reads and writes go through `run_worker_session`, which copes with
being called both from plain sync code and from inside a running event loop.
That second case is not exotic: most Celery tasks are shaped as
`run_async(with_worker_session(_handle))` and then call the synchronous Gmail
wrappers from within `_handle`, which lands here under a live loop.

Because `with_worker_session` builds and disposes an engine per call, a
short-lived in-process cache sits in front of it: one Celery task making thirty
Gmail calls should pay for one connection read, not thirty.
"""

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from core.crypto import DecryptionFailed, decrypt, encrypt
from core.database import run_worker_session
from core.logging import get_logger
from core.redis import sync_redis
from integrations.google.oauth import (
    GrantRevoked,
    RefreshedToken,
    TokenGrant,
    refresh_access_token,
)
from models.google import (
    CALENDAR_REQUIRED_SCOPES,
    GMAIL_REQUIRED_SCOPES,
    expand_legacy_scopes,
    GoogleConnection,
)

log = get_logger(__name__)

# How long a connection snapshot may be served from process memory. Short
# enough that a revocation or a scope change takes effect within a minute,
# long enough to collapse one task's many Gmail calls onto one DB read.
_SNAPSHOT_TTL = 60.0

# Treat a token as expired while it still has this long to live, so a call that
# starts just under the wire does not race the expiry mid-flight.
_EXPIRY_MARGIN = timedelta(seconds=120)

# Refresh lock. Concurrent redemption of the same refresh token is not actually
# harmful — Google keeps previously issued access tokens valid — so this exists
# to stop N workers making N identical refresh calls, not to protect
# correctness. Hence the short TTL and the willingness to give up waiting.
_LOCK_TTL = 30
_LOCK_WAIT_SECONDS = 3.0
_LOCK_POLL_SECONDS = 0.2


class NotConnected(RuntimeError):
    """The user has no usable Google grant."""


class ScopeMissing(RuntimeError):
    """The grant exists but does not cover what this call needs."""


@dataclass(frozen=True)
class GoogleCredentials:
    """A live access token plus what it is allowed to do."""

    user_id: str
    access_token: str
    email: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class ConnectionState:
    """A decrypted snapshot of a `google_connections` row."""

    user_id: str
    email: str
    google_sub: str
    scopes: frozenset[str]
    history_id: str | None
    watch_expires_at: datetime | None
    revoked: bool
    access_token: str | None
    refresh_token: str
    token_expiry: datetime | None

    @property
    def token_is_live(self) -> bool:
        if not self.access_token or not self.token_expiry:
            return False
        return self.token_expiry > datetime.now(timezone.utc) + _EXPIRY_MARGIN


# user_id -> (cached_at_monotonic, snapshot). Deliberately unbounded-in-theory
# but bounded in practice by the number of users a single worker touches.
_snapshots: dict[str, tuple[float, ConnectionState | None]] = {}


def invalidate(user_id: str) -> None:
    """Drop the cached snapshot, forcing the next read to hit the database."""
    _snapshots.pop(user_id, None)


def invalidate_all() -> None:
    _snapshots.clear()


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def get_connection(user_id: str, *, fresh: bool = False) -> ConnectionState | None:
    """The user's connection, or None if they have never connected.

    A revoked connection is still returned — callers that care check `.revoked`.
    That distinction matters for the UI, which wants to say "reconnect your
    account" rather than "connect an account".
    """
    if not fresh:
        cached = _snapshots.get(user_id)
        if cached and (time.monotonic() - cached[0]) < _SNAPSHOT_TTL:
            return cached[1]

    state: ConnectionState | None = run_worker_session(lambda db: _load(db, user_id))
    _snapshots[user_id] = (time.monotonic(), state)
    return state


def is_connected(user_id: str, required: frozenset[str]) -> bool:
    """Whether the user holds a live grant covering `required`."""
    state = get_connection(user_id)
    return bool(state and not state.revoked and required <= state.scopes)


def gmail_connected(user_id: str) -> bool:
    return is_connected(user_id, GMAIL_REQUIRED_SCOPES)


def calendar_connected(user_id: str) -> bool:
    return is_connected(user_id, CALENDAR_REQUIRED_SCOPES)


def get_credentials(user_id: str, required: frozenset[str] | None = None) -> GoogleCredentials:
    """A live access token for `user_id`, refreshing it if necessary.

    Raises `NotConnected` when there is no usable grant, `ScopeMissing` when the
    grant does not cover `required`, and `GrantRevoked` when Google has rejected
    the refresh token.
    """
    state = get_connection(user_id)
    if state is None:
        raise NotConnected(f"user {user_id} has not connected Google")
    if state.revoked:
        raise GrantRevoked(f"user {user_id} must reconnect Google")
    if required and not required <= state.scopes:
        missing = " ".join(sorted(required - state.scopes))
        raise ScopeMissing(f"grant for user {user_id} is missing: {missing}")

    if state.token_is_live:
        return GoogleCredentials(
            user_id=user_id,
            access_token=state.access_token or "",
            email=state.email,
            scopes=state.scopes,
        )

    return _refresh(user_id, state)


def force_refresh(user_id: str) -> GoogleCredentials:
    """Mint a new access token even if the stored one looks live.

    For the 401 retry path: Google can reject a token we believe is valid (an
    administrator revoking a session, a clock skew), and the only recovery is to
    stop believing our own expiry.
    """
    invalidate(user_id)
    state = get_connection(user_id, fresh=True)
    if state is None:
        raise NotConnected(f"user {user_id} has not connected Google")
    if state.revoked:
        raise GrantRevoked(f"user {user_id} must reconnect Google")
    return _refresh(user_id, state, force=True)


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------


def _refresh(user_id: str, state: ConnectionState, *, force: bool = False) -> GoogleCredentials:
    """Refresh under a Redis lock, falling back to doing it ourselves."""
    lock_key = f"lock:gcred:{user_id}"
    client = sync_redis()

    got_lock = False
    try:
        got_lock = bool(client.set(lock_key, "1", nx=True, ex=_LOCK_TTL))
    except Exception:
        # Redis being down must not take Gmail down with it. Without the lock we
        # may duplicate a refresh, which costs a request and nothing else.
        log.warning("google.refresh_lock_unavailable", user_id=user_id, exc_info=True)

    if not got_lock:
        if winner := _await_refresh(user_id, state, force=force):
            return winner
        # Nobody produced a token in time; refresh anyway rather than block a
        # worker indefinitely behind a lock that may belong to a dead process.
        log.info("google.refresh_lock_timeout", user_id=user_id)

    try:
        return _do_refresh(user_id, state)
    finally:
        if got_lock:
            try:
                client.delete(lock_key)
            except Exception:
                log.warning("google.refresh_unlock_failed", user_id=user_id, exc_info=True)


def _await_refresh(
    user_id: str, state: ConnectionState, *, force: bool
) -> GoogleCredentials | None:
    """Poll the database for a token another worker just wrote."""
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_LOCK_POLL_SECONDS)
        fresh = get_connection(user_id, fresh=True)
        if fresh is None or fresh.revoked:
            return None
        # On the forced path the token we already had also "looks live", so a
        # snapshot only counts as the winner's work if it is genuinely newer.
        if force and fresh.token_expiry == state.token_expiry:
            continue
        if fresh.token_is_live:
            return GoogleCredentials(
                user_id=user_id,
                access_token=fresh.access_token or "",
                email=fresh.email,
                scopes=fresh.scopes,
            )
    return None


def _do_refresh(user_id: str, state: ConnectionState) -> GoogleCredentials:
    try:
        token = refresh_access_token(state.refresh_token)
    except GrantRevoked as exc:
        mark_revoked(user_id, str(exc))
        raise

    expiry = datetime.now(timezone.utc) + timedelta(seconds=token.expires_in)
    # The stored column holds exactly what Google returned, so only a refresh
    # that actually carried scopes may rewrite it — passing None leaves it be.
    # The expansion is applied to the in-memory copy only, which is what every
    # scope check reads; it is idempotent, so a `state.scopes` that `_load`
    # already expanded passes through unchanged.
    scopes = expand_legacy_scopes(token.scopes or state.scopes)
    _persist_refreshed(user_id, token, expiry, token.scopes or None)
    invalidate(user_id)

    log.info("google.token_refreshed", user_id=user_id, expires_in=token.expires_in)
    return GoogleCredentials(
        user_id=user_id,
        access_token=token.access_token,
        email=state.email,
        scopes=scopes,
    )


def _persist_refreshed(
    user_id: str, token: RefreshedToken, expiry: datetime, scopes: frozenset[str] | None
) -> None:
    """Write the new token, without clobbering a newer one.

    The guard on `token_expiry` is what makes a slow refresh safe: two workers
    can refresh concurrently, and the one whose response arrives late must not
    overwrite the fresher token that landed while it was in flight.

    A rotated *refresh* token is written unconditionally. Google keeps both the
    old and new one valid through the rotation, so last-writer-wins is fine and
    losing the new one would be worse.
    """

    async def _write(db) -> None:
        values: dict = {
            "access_token": encrypt(token.access_token),
            "token_expiry": expiry,
            "last_error": None,
        }
        # `None` means this refresh returned no scope list — Google omits it
        # when nothing changed. Leave the stored grant alone rather than
        # writing a guess over it.
        if scopes is not None:
            values["scopes"] = " ".join(sorted(scopes))
        await db.execute(
            update(GoogleConnection)
            .where(
                GoogleConnection.user_id == uuid.UUID(user_id),
                (GoogleConnection.token_expiry.is_(None))
                | (GoogleConnection.token_expiry < expiry),
            )
            .values(**values)
        )
        if token.refresh_token:
            await db.execute(
                update(GoogleConnection)
                .where(GoogleConnection.user_id == uuid.UUID(user_id))
                .values(refresh_token=encrypt(token.refresh_token))
            )

    run_worker_session(_write)


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def store_grant(user_id: str, grant: TokenGrant) -> None:
    """Create or replace the user's connection from a completed consent."""
    expiry = datetime.now(timezone.utc) + timedelta(seconds=grant.expires_in)

    async def _write(db) -> None:
        existing = await db.scalar(
            select(GoogleConnection).where(GoogleConnection.user_id == uuid.UUID(user_id))
        )
        if existing is None:
            db.add(
                GoogleConnection(
                    user_id=uuid.UUID(user_id),
                    google_sub=grant.google_sub,
                    email=grant.email,
                    access_token=encrypt(grant.access_token),
                    refresh_token=encrypt(grant.refresh_token or ""),
                    token_expiry=expiry,
                    scopes=" ".join(sorted(grant.scopes)),
                )
            )
            return

        existing.google_sub = grant.google_sub
        existing.email = grant.email
        existing.access_token = encrypt(grant.access_token)
        if grant.refresh_token:
            existing.refresh_token = encrypt(grant.refresh_token)
        existing.token_expiry = expiry
        existing.scopes = " ".join(sorted(grant.scopes))
        existing.last_error = None
        # Reconnecting is the documented fix for a dead grant, so it has to
        # actually clear the flag that took the user out of the poll.
        existing.revoked_at = None
        # The cursor is deliberately left alone. A reconnecting user's mailbox
        # kept moving while they were disconnected, and their old cursor is
        # still the correct place to resume from; the poller resets it itself if
        # Gmail says it has aged out.

    run_worker_session(_write)
    invalidate(user_id)


def mark_revoked(user_id: str, reason: str) -> None:
    """Record that the grant is dead, so the poller stops and the UI can prompt.

    The durable flag matters more than the exception that accompanies it:
    several callers swallow broad exceptions, so this row is the only thing that
    reliably takes a dead account out of the fan-out.
    """

    async def _write(db) -> None:
        await db.execute(
            update(GoogleConnection)
            .where(GoogleConnection.user_id == uuid.UUID(user_id))
            .values(
                revoked_at=datetime.now(timezone.utc),
                last_error=reason[:500],
                access_token=None,
            )
        )

    try:
        run_worker_session(_write)
    except Exception:
        log.exception("google.mark_revoked_failed", user_id=user_id)
    finally:
        invalidate(user_id)
    log.warning("google.grant_revoked", user_id=user_id, reason=reason)


def set_history_id(user_id: str, history_id: str, *, only_if_unset: bool = False) -> None:
    """Seed or reset the mailbox cursor."""

    async def _write(db) -> None:
        stmt = update(GoogleConnection).where(GoogleConnection.user_id == uuid.UUID(user_id))
        if only_if_unset:
            stmt = stmt.where(GoogleConnection.history_id.is_(None))
        await db.execute(stmt.values(history_id=history_id))

    run_worker_session(_write)
    invalidate(user_id)


def advance_history_id(user_id: str, *, previous: str | None, new: str) -> bool:
    """Move the cursor forward, only if it is still where we left it.

    Returns False when another worker moved it first — which is a normal
    outcome, not an error: that worker enqueued the same messages, and the
    idempotency claim makes the overlap a no-op.
    """

    async def _write(db) -> int:
        stmt = (
            update(GoogleConnection)
            .where(
                GoogleConnection.user_id == uuid.UUID(user_id),
                GoogleConnection.history_id.is_(None)
                if previous is None
                else GoogleConnection.history_id == previous,
            )
            .values(history_id=new, last_polled_at=datetime.now(timezone.utc))
        )
        result = await db.execute(stmt)
        return result.rowcount or 0

    moved = run_worker_session(_write) > 0
    invalidate(user_id)
    return moved


def set_watch_expiry(user_id: str, expires_at: datetime | None) -> None:
    """Record when this mailbox's Gmail push watch lapses."""

    async def _write(db) -> None:
        await db.execute(
            update(GoogleConnection)
            .where(GoogleConnection.user_id == uuid.UUID(user_id))
            .values(watch_expires_at=expires_at)
        )

    run_worker_session(_write)
    invalidate(user_id)


def find_user_id_by_mailbox(email: str) -> str | None:
    """Which app user granted access to this mailbox.

    Gmail's push notification identifies the mailbox by address and nothing
    else, so this is the only way back to a user id from a notification.
    Matched case-insensitively — Google echoes the address in whatever case the
    grant carried, which need not match what we stored.
    """

    async def _read(db) -> str | None:
        found = await db.scalar(
            select(GoogleConnection.user_id).where(
                func.lower(GoogleConnection.email) == email.lower(),
                GoogleConnection.revoked_at.is_(None),
            )
        )
        return str(found) if found else None

    return run_worker_session(_read)


def list_watches_needing_renewal(before: datetime) -> list[str]:
    """Live connections whose watch lapses before `before`, or has none.

    A NULL expiry is included on purpose: that is a mailbox which has never had
    a watch installed — a user who connected while push was switched off, or
    one whose install failed — and it needs one just as much as a lapsing one.
    """

    async def _read(db) -> list[str]:
        rows = await db.execute(
            select(GoogleConnection.user_id).where(
                GoogleConnection.revoked_at.is_(None),
                GoogleConnection.history_id.is_not(None),
                (GoogleConnection.watch_expires_at.is_(None))
                | (GoogleConnection.watch_expires_at < before),
            )
        )
        return [str(row[0]) for row in rows.all()]

    return run_worker_session(_read)


def list_pollable_user_ids() -> list[str]:
    """Every user the mailbox poller should visit."""

    async def _read(db) -> list[str]:
        rows = await db.execute(
            select(GoogleConnection.user_id).where(
                GoogleConnection.revoked_at.is_(None),
                GoogleConnection.history_id.is_not(None),
            )
        )
        return [str(row[0]) for row in rows.all()]

    return run_worker_session(_read)


def disconnect(user_id: str) -> None:
    """Forget the grant entirely (user-initiated disconnect)."""

    async def _write(db) -> None:
        existing = await db.scalar(
            select(GoogleConnection).where(GoogleConnection.user_id == uuid.UUID(user_id))
        )
        if existing is not None:
            await db.delete(existing)

    run_worker_session(_write)
    invalidate(user_id)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


async def _load(db, user_id: str) -> ConnectionState | None:
    try:
        key = uuid.UUID(user_id)
    except ValueError:
        return None

    row = await db.scalar(select(GoogleConnection).where(GoogleConnection.user_id == key))
    if row is None:
        return None

    try:
        refresh_token = decrypt(row.refresh_token) if row.refresh_token else ""
        access_token = decrypt(row.access_token) if row.access_token else None
    except DecryptionFailed:
        # A row we cannot read is a row the user has to re-grant. Surfacing it as
        # "revoked" routes it into the existing reconnect prompt instead of
        # failing every Gmail call with a cryptography error nobody can action.
        log.error("google.token_undecryptable", user_id=user_id)
        return ConnectionState(
            user_id=user_id,
            email=row.email,
            google_sub=row.google_sub,
            scopes=frozenset(),
            history_id=row.history_id,
            watch_expires_at=row.watch_expires_at,
            revoked=True,
            access_token=None,
            refresh_token="",
            token_expiry=None,
        )

    return ConnectionState(
        user_id=user_id,
        email=row.email,
        google_sub=row.google_sub,
        scopes=expand_legacy_scopes(frozenset(row.scopes.split())),
        history_id=row.history_id,
        watch_expires_at=row.watch_expires_at,
        revoked=row.revoked_at is not None,
        access_token=access_token,
        refresh_token=refresh_token,
        token_expiry=row.token_expiry,
    )
