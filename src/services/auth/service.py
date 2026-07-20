import asyncio
from urllib.parse import urlencode

import httpx
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from core.config import settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def build_authorization_url(state: str, code_challenge: str) -> str:
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_profile(code: str, code_verifier: str) -> dict:
    """Exchange the auth code, verify the ID token, return the verified profile."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
        )
        resp.raise_for_status()
        raw_id_token = resp.json()["id_token"]

    # verify_oauth2_token does blocking network I/O (fetches Google's certs), so
    # run it off the event loop. It checks signature, audience, issuer, expiry.
    info = await asyncio.to_thread(
        google_id_token.verify_oauth2_token,
        raw_id_token,
        google_requests.Request(),
        settings.GOOGLE_CLIENT_ID,
    )
    if not info.get("email_verified"):
        raise ValueError("Google email not verified")

    return {
        "sub": info["sub"],
        "email": info["email"],
        "name": info.get("name"),
        "picture": info.get("picture"),
    }
