"""Print the Google consent URL for a user, without going through the web app.

Useful for connecting an account from a terminal — during setup, when
reconnecting a revoked grant, or before the frontend's connect button is
wired up. Paste the URL into a browser, consent, and the callback stores the
grant exactly as the real route would.

    docker compose exec api python scripts/google_connect_url.py you@example.com

Issues a real single-use state token (10 minute TTL), so the callback's CSRF
check passes. Read-only apart from that token.
"""

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import select  # noqa: E402

from core.crypto import is_configured  # noqa: E402
from core.database import run_async, with_worker_session  # noqa: E402
from core.redis import sync_redis  # noqa: E402
from integrations.google.oauth import build_connect_url, callback_url  # noqa: E402
from models.users import User  # noqa: E402

STATE_PREFIX = "goauth:"
STATE_TTL_SECONDS = 600


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="the app user to attach the grant to")
    args = parser.parse_args()

    if not is_configured():
        sys.exit(
            "GOOGLE_TOKEN_ENCRYPTION_KEYS is not set — the callback would be unable "
            "to store the tokens. Generate a key with:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )

    async def _read(db):
        user = await db.scalar(select(User).where(User.email == args.email))
        return (str(user.id), user.email) if user else None

    found = run_async(with_worker_session(_read))
    if not found:
        sys.exit(f"no user with email {args.email!r}")
    user_id, email = found

    state = secrets.token_urlsafe(32)
    sync_redis().set(f"{STATE_PREFIX}{state}", user_id, ex=STATE_TTL_SECONDS)

    print(f"\nuser:     {email} ({user_id})")
    print(f"callback: {callback_url()}")
    print("\nThis exact callback URL must be registered as an authorised redirect URI")
    print("on the OAuth client in the Google Cloud console, or consent will fail.\n")
    print("Open this, and consent as that same Google account:\n")
    print(build_connect_url(state, login_hint=email))
    print(f"\n(valid for {STATE_TTL_SECONDS // 60} minutes)\n")


if __name__ == "__main__":
    main()
