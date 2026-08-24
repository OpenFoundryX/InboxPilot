"""The Google OAuth grant that covers Gmail and Calendar together.

Separate from the app's *login* grant (`services.auth.service`), which asks only
for `openid email profile` and keeps the consent screen light at signup. This is
the second, heavier grant: the one that actually authorises reading and writing
the user's mail and calendar, requested once from the Connect step.

Synchronous — call from a Celery task, or from async code via
`run_in_threadpool`, matching how the rest of the integration layer works.
"""

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from core.config import settings
from core.logging import get_logger
from models.google import CONNECT_SCOPES

log = get_logger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

HTTP_TIMEOUT = 15.0


class GoogleOAuthError(RuntimeError):
    """The grant could not be established or refreshed."""


class GrantRevoked(GoogleOAuthError):
    """Google rejected the refresh token; the user must reconnect.

    Google returns `invalid_grant` for several distinct situations that all
    require the same fix: the user revoked access, changed their password, left
    the grant idle for six months — or the OAuth app is still in "Testing"
    publishing status, where refresh tokens expire after seven days. That last
    one is a development nuisance rather than a bug, and is worth remembering
    before debugging a "working yesterday" report.
    """


@dataclass(frozen=True)
class TokenGrant:
    """What Google hands back from an authorization-code exchange."""

    access_token: str
    refresh_token: str | None
    expires_in: int
    scopes: frozenset[str]
    google_sub: str
    email: str


@dataclass(frozen=True)
class RefreshedToken:
    access_token: str
    expires_in: int
    # Google usually omits this and keeps the existing refresh token valid; it
    # is only present when the token actually rotated.
    refresh_token: str | None
    scopes: frozenset[str]


def callback_url() -> str:
    """Where Google returns the browser after consent.

    Must be publicly reachable and registered verbatim as an authorised redirect
    URI in the Google Cloud console, hence PUBLIC_BASE_URL rather than a
    localhost address.
    """
    base = f"{settings.PUBLIC_BASE_URL.rstrip('/')}{settings.API_V1_PREFIX}"
    return f"{base}/integrations/google/callback"


def build_connect_url(state: str, login_hint: str | None = None) -> str:
    """The consent-screen URL for the Gmail + Calendar grant.

    Three parameters here are load-bearing:

    * `access_type=offline` and `prompt=consent` together are what produce a
      refresh token. Without `prompt=consent` Google returns one only on the
      user's *first* ever consent, so anyone who has already granted these
      scopes — including anyone reconnecting after a revocation — would come
      back with an access token that expires in an hour and no way to renew it.
    * `include_granted_scopes=true` makes this incremental: the resulting grant
      carries the login scopes as well, rather than replacing them.
    * `login_hint` steers the picker at the account the user is signed in as.
      It is a hint, not a constraint — the callback still verifies the account
      that came back is the right one.
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise GoogleOAuthError("GOOGLE_CLIENT_ID is not configured")

    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": callback_url(),
        "response_type": "code",
        "scope": " ".join(CONNECT_SCOPES),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> TokenGrant:
    """Trade an authorization code for tokens and the granted identity."""
    payload = {
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": callback_url(),
        "grant_type": "authorization_code",
    }
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.post(TOKEN_URL, data=payload)

    if resp.status_code != 200:
        raise GoogleOAuthError(f"token exchange failed ({resp.status_code}): {_error_text(resp)}")

    body = resp.json()
    refresh_token = body.get("refresh_token")
    if not refresh_token:
        # `prompt=consent` should make this impossible. If it happens anyway the
        # grant is useless to us — an access token alone dies in an hour and no
        # background sweep would ever work again — so fail loudly here rather
        # than storing a row that breaks silently at 3am.
        raise GoogleOAuthError(
            "Google returned no refresh token; the grant cannot be used for background access"
        )

    info = _verify_id_token(body.get("id_token"))
    return TokenGrant(
        access_token=body["access_token"],
        refresh_token=refresh_token,
        expires_in=int(body.get("expires_in", 3600)),
        scopes=frozenset((body.get("scope") or "").split()),
        google_sub=info["sub"],
        email=info["email"],
    )


def refresh_access_token(refresh_token: str) -> RefreshedToken:
    """Mint a fresh access token. Raises `GrantRevoked` if the grant is dead."""
    payload = {
        "refresh_token": refresh_token,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "grant_type": "refresh_token",
    }
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.post(TOKEN_URL, data=payload)

    if resp.status_code == 400:
        body = _safe_json(resp)
        if body.get("error") == "invalid_grant":
            raise GrantRevoked(str(body.get("error_description") or "invalid_grant"))

    if resp.status_code != 200:
        raise GoogleOAuthError(f"token refresh failed ({resp.status_code}): {_error_text(resp)}")

    body = resp.json()
    return RefreshedToken(
        access_token=body["access_token"],
        expires_in=int(body.get("expires_in", 3600)),
        refresh_token=body.get("refresh_token"),
        scopes=frozenset((body.get("scope") or "").split()),
    )


def revoke(token: str) -> None:
    """Ask Google to invalidate a token. Best-effort; never raises.

    Used when a user disconnects. An already-invalid token comes back 400, which
    is the desired end state rather than a failure.
    """
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            client.post(REVOKE_URL, data={"token": token})
    except Exception:
        log.warning("google.revoke_failed", exc_info=True)


def _verify_id_token(raw: str | None) -> dict:
    """Verify the id_token's signature and audience, returning its claims.

    The identity in here is what binds the grant to an app user. Trusting the
    unverified payload would let anyone who can reach the callback claim any
    mailbox, so this is not optional.
    """
    if not raw:
        raise GoogleOAuthError("Google returned no id_token")

    try:
        info = google_id_token.verify_oauth2_token(
            raw, google_requests.Request(), settings.GOOGLE_CLIENT_ID
        )
    except ValueError as exc:
        raise GoogleOAuthError(f"id_token verification failed: {exc}") from exc

    if not info.get("email_verified"):
        raise GoogleOAuthError("Google email not verified")
    return info


def _safe_json(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_text(resp: httpx.Response) -> str:
    """A short, log-safe description of a failed token call.

    Deliberately narrow: these responses sit next to client secrets and codes,
    so only Google's own error fields are surfaced, never the whole body.
    """
    body = _safe_json(resp)
    if error := body.get("error"):
        description = body.get("error_description")
        return f"{error}: {description}" if description else str(error)
    return "unrecognised error response"
