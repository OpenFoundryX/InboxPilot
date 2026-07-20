"""Authentication routes (API v1) — Google OAuth (PKCE) + refresh sessions."""

import base64
import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from api.deps import DbSession
from core.config import settings
from models.users import User
from schemas.user import UserRead
from services.auth import oauth, service
from services.auth.dependencies import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])

# Short-lived cookies that carry the PKCE verifier + CSRF state across the
# round-trip to Google, and the session cookies set after login.
STATE_COOKIE = "oauth_state"
VERIFIER_COOKIE = "oauth_verifier"
ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"
TRANSIENT_MAX_AGE = 600  # 10 minutes to complete the Google login


def _generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for the S256 PKCE flow."""
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def _cookie_kwargs() -> dict:
    # secure=False in local so cookies work over http; https-only elsewhere.
    return {
        "httponly": True,
        "secure": settings.ENVIRONMENT != "local",
        "samesite": "lax",
        "path": "/",
    }


def _set_session_cookies(resp: Response, access: str, refresh: str) -> None:
    resp.set_cookie(
        ACCESS_COOKIE, access, max_age=settings.ACCESS_TOKEN_TTL_MIN * 60, **_cookie_kwargs()
    )
    resp.set_cookie(
        REFRESH_COOKIE,
        refresh,
        max_age=settings.REFRESH_TOKEN_TTL_DAYS * 86400,
        **_cookie_kwargs(),
    )


@router.get("/google/login")
async def google_login() -> RedirectResponse:
    """Start the flow: redirect to Google with PKCE challenge + CSRF state."""
    state = secrets.token_urlsafe(32)
    verifier, challenge = _generate_pkce()
    url = service.build_authorization_url(state, challenge)

    resp = RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    resp.set_cookie(STATE_COOKIE, state, max_age=TRANSIENT_MAX_AGE, **_cookie_kwargs())
    resp.set_cookie(VERIFIER_COOKIE, verifier, max_age=TRANSIENT_MAX_AGE, **_cookie_kwargs())
    return resp


@router.get("/google/callback")
async def google_callback(
    request: Request, db: DbSession, code: str, state: str
) -> RedirectResponse:
    """Handle Google's redirect: verify state, exchange code, log the user in."""
    cookie_state = request.cookies.get(STATE_COOKIE)
    verifier = request.cookies.get(VERIFIER_COOKIE)
    if not cookie_state or not verifier or not secrets.compare_digest(cookie_state, state):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid OAuth state")

    try:
        profile = await service.exchange_code_for_profile(code, verifier)
    except Exception as exc:  # httpx errors, missing id_token, unverified email
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Google authentication failed"
        ) from exc

    user = await oauth.upsert_user_from_google(db, profile)
    access, refresh = await oauth.issue_tokens(db, user)

    resp = RedirectResponse(
        settings.POST_LOGIN_REDIRECT_URL, status_code=status.HTTP_303_SEE_OTHER
    )
    _set_session_cookies(resp, access, refresh)
    resp.delete_cookie(STATE_COOKIE, path="/")
    resp.delete_cookie(VERIFIER_COOKIE, path="/")
    return resp


@router.post("/refresh", status_code=status.HTTP_204_NO_CONTENT)
async def refresh(request: Request, db: DbSession) -> Response:
    """Rotate the refresh token and issue a fresh access token."""
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No refresh token")

    tokens = await oauth.rotate_refresh_token(db, raw)
    if tokens is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")

    access, new_refresh = tokens
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    _set_session_cookies(resp, access, new_refresh)
    return resp


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(db: DbSession, user: User = Depends(get_current_user)) -> Response:
    """Revoke all of the user's refresh tokens and clear the session cookies."""
    await oauth.revoke_all(db, user.id)
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    resp.delete_cookie(ACCESS_COOKIE, path="/")
    resp.delete_cookie(REFRESH_COOKIE, path="/")
    return resp


@router.get("/me", response_model=UserRead)
async def me(user: User = Depends(get_current_user)) -> User:
    return user
