"""Dev helpers for exercising the meeting notetaker without a real meeting.

Two things are awkward to do by hand: minting a bearer token, and signing a
provider webhook (the endpoint verifies HMAC, so `curl` alone always 401s).

Run from the repo root, on the host:

    PYTHONPATH=src uv run python scripts/notetaker_dev.py token <user_id>
    PYTHONPATH=src uv run python scripts/notetaker_dev.py webhook <bot_id> done

Neither subcommand touches the database — ids come from you, so this works
against a dockerised stack from outside it.
"""

import argparse
import base64
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request

from core.config import settings
from core.security import create_access_token

# Codes the endpoint acts on. `done` is the one that triggers the recap.
CODES = (
    "joining_call",
    "in_waiting_room",
    "in_call_recording",
    "call_ended",
    "done",
    "fatal",
    "recording_permission_denied",
)


def cmd_token(args) -> int:
    print(create_access_token(args.user_id))
    return 0


def cmd_webhook(args) -> int:
    secret = settings.RECALL_WEBHOOK_SECRET
    if not secret:
        print("RECALL_WEBHOOK_SECRET is not set in .env — the endpoint will 401.", file=sys.stderr)
        return 2

    body = json.dumps(
        {
            "event": f"bot.{args.code}",
            "data": {
                "data": {
                    "code": args.code,
                    "sub_code": args.sub_code,
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                "bot": {
                    "id": args.bot_id,
                    "metadata": ({"meeting_id": args.meeting_id} if args.meeting_id else {}),
                },
            },
        }
    ).encode()

    msg_id = f"msg_dev_{int(time.time())}"
    timestamp = str(int(time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{msg_id}.{timestamp}.".encode() + body
    signature = "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()

    url = f"{args.base_url.rstrip('/')}/v1/webhooks/meeting-bot"
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "webhook-id": msg_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": signature,
        },
    )
    try:
        with urllib.request.urlopen(request) as resp:
            print(f"{resp.status} {resp.read().decode()}")
    except urllib.error.HTTPError as exc:
        print(f"{exc.code} {exc.read().decode()}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    token = sub.add_parser("token", help="mint a bearer token for a user id")
    token.add_argument("user_id")
    token.set_defaults(func=cmd_token)

    hook = sub.add_parser("webhook", help="send a signed provider status callback")
    hook.add_argument("bot_id")
    hook.add_argument("code", choices=CODES)
    hook.add_argument("--meeting-id", help="echoed as bot metadata, as the real provider does")
    hook.add_argument("--sub-code", default=None)
    hook.add_argument("--base-url", default="http://localhost:8000")
    hook.set_defaults(func=cmd_webhook)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
