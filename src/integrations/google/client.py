"""Authorized HTTP against Google's APIs.

One function — `google_request` — carries the token handling, the retry policy,
and the mapping from Google's error envelope onto typed exceptions, so the Gmail
and Calendar wrappers can read as plain endpoint calls.

Synchronous and blocking, like everything else in this package.
"""

import random
import time
from typing import Any

import httpx

from core.config import settings
from core.logging import get_logger
from integrations.google.credentials import (
    ScopeMissing,
    force_refresh,
    get_credentials,
)

log = get_logger(__name__)

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"

MAX_ATTEMPTS = 4
BASE_BACKOFF = 0.5

# Google's `reason` values that mean "slow down" rather than "you may not".
_RATE_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "backendError"}
)
_SCOPE_REASONS = frozenset({"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})


class GoogleAPIError(RuntimeError):
    """A Google API call failed."""

    def __init__(self, message: str, *, status_code: int | None = None, reason: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class GoogleNotFound(GoogleAPIError):
    """The resource does not exist, or is already gone.

    Typed because deleting something that has already been deleted is the
    caller's desired end state, not a failure — and matching on error strings to
    work that out is what the Composio layer had to do.
    """


class GoogleRateLimited(GoogleAPIError):
    """Still rate limited after exhausting the retry budget."""


def google_request(
    user_id: str,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json: Any = None,
    data: bytes | None = None,
    headers: dict | None = None,
    required_scopes: frozenset[str] | None = None,
    expect_json: bool = True,
    idempotent: bool = True,
    timeout: float | None = None,
) -> Any:
    """Make an authorized call, refreshing and retrying as needed.

    `idempotent=False` disables *all* automatic retries, including on timeouts
    and 5xx. Use it for anything that creates something the user can see — a
    sent message, a draft, a calendar invite. A send that times out has very
    often already been delivered, and retrying it emails the recipient twice;
    an occasional lost send is recoverable, a duplicate one is not.

    `expect_json=False` for the endpoints that answer 204 with an empty body
    (batchModify, the deletes), where parsing the response would itself be the
    bug.
    """
    creds = get_credentials(user_id, required_scopes)
    token = creds.access_token
    refreshed = False
    last_error: Exception | None = None

    for attempt in range(MAX_ATTEMPTS):
        request_headers = {"Authorization": f"Bearer {token}", **(headers or {})}
        try:
            response = _client().request(
                method,
                url,
                params=params,
                json=json,
                content=data,
                headers=request_headers,
                timeout=timeout or settings.GOOGLE_HTTP_TIMEOUT,
            )
        except httpx.TimeoutException as exc:
            last_error = exc
            if not idempotent or attempt == MAX_ATTEMPTS - 1:
                raise GoogleAPIError(f"{method} {url} timed out") from exc
            _sleep(attempt, "timeout", user_id)
            continue
        except httpx.HTTPError as exc:
            last_error = exc
            if not idempotent or attempt == MAX_ATTEMPTS - 1:
                raise GoogleAPIError(f"{method} {url} failed: {exc}") from exc
            _sleep(attempt, "transport", user_id)
            continue

        if response.is_success:
            return _parse(response, expect_json)

        status = response.status_code
        reason, message = _error_details(response)

        # An access token we believed was live can still be rejected. Refresh
        # once and retry — but only once, or a genuinely bad grant becomes a
        # refresh loop.
        if status == 401 and not refreshed:
            refreshed = True
            token = force_refresh(user_id).access_token
            log.info("google.token_reauthorized", user_id=user_id, url=url)
            continue

        if status == 404:
            raise GoogleNotFound(f"{method} {url}: {message}", status_code=404, reason=reason)

        if status == 403 and reason in _SCOPE_REASONS:
            raise ScopeMissing(f"{method} {url}: {message}")

        retryable = status == 429 or status >= 500 or (status == 403 and reason in _RATE_REASONS)
        if retryable and idempotent and attempt < MAX_ATTEMPTS - 1:
            _sleep(attempt, reason or str(status), user_id, response=response)
            continue

        if status == 429 or (status == 403 and reason in _RATE_REASONS):
            raise GoogleRateLimited(
                f"{method} {url}: {message}", status_code=status, reason=reason
            )

        raise GoogleAPIError(f"{method} {url}: {message}", status_code=status, reason=reason)

    raise GoogleAPIError(f"{method} {url} exhausted retries: {last_error}")


def _parse(response: httpx.Response, expect_json: bool) -> Any:
    if not expect_json or response.status_code == 204 or not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


def _error_details(response: httpx.Response) -> tuple[str, str]:
    """Pull (reason, message) out of Google's error envelope."""
    try:
        body = response.json()
    except ValueError:
        return "", response.text[:200]

    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, str):
        return error, str(body.get("error_description") or error)
    if not isinstance(error, dict):
        return "", str(body)[:200]

    message = str(error.get("message") or "")
    reason = ""
    details = error.get("errors")
    if isinstance(details, list) and details and isinstance(details[0], dict):
        reason = str(details[0].get("reason") or "")
    if not reason:
        reason = str(error.get("status") or "")
    return reason, message or reason


def _sleep(attempt: int, reason: str, user_id: str, response: httpx.Response | None = None) -> None:
    """Back off with full jitter, honouring Retry-After when Google sends one."""
    delay = BASE_BACKOFF * (2**attempt)
    if response is not None and (retry_after := response.headers.get("Retry-After")):
        try:
            delay = max(delay, float(retry_after))
        except ValueError:
            pass
    # Full jitter: without it, every worker that hit the same limit at the same
    # moment retries at the same moment and re-creates the burst it is backing
    # off from.
    wait = random.uniform(0, delay)
    log.warning("google.retry", user_id=user_id, reason=reason, attempt=attempt + 1, wait=round(wait, 2))
    time.sleep(wait)


_shared_client: httpx.Client | None = None


def _client() -> httpx.Client:
    """The process-wide HTTP client.

    Built lazily rather than at import: Celery's prefork model would otherwise
    hand every child a copy of the parent's connection pool, and two processes
    reading the same socket produce failures that look like corrupted responses.
    """
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.Client(
            timeout=settings.GOOGLE_HTTP_TIMEOUT,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _shared_client


def reset_client() -> None:
    """Drop the shared client (test teardown, and after a fork)."""
    global _shared_client
    if _shared_client is not None:
        _shared_client.close()
        _shared_client = None
