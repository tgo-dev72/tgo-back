#!/usr/bin/env python3
"""YouCanPay private-key check. Python 3.9+, no dependencies.

Run in a local terminal: python3 scripts/check_youcanpay_key.py
Sandbox keys create a test token, never a charge. Live keys use a read-only GET.
Keys and payment tokens are never intentionally written to disk or logged.
Secrets exist in process memory; Python cannot guarantee secure memory erasure.
"""

import base64
import getpass
import json
import socket
import ssl
import sys
import warnings
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import (
    HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener,
)

ENDPOINT = "https://youcanpay.com/api/v2/account/me"
SANDBOX_ENDPOINT = "https://youcanpay.com/sandbox/api/tokenize"
MAX_RESPONSE_BYTES = 64 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a credential to a redirected destination.
        return None


def check_key(private_key):
    if not private_key or len(private_key) > 4096 or any(
        char.isspace() or ord(char) < 32 for char in private_key
    ) or ":" in private_key:
        print("NOT TESTED: empty or malformed input.")
        return 2

    sandbox = private_key.startswith("pri_sandbox_")
    if sandbox:
        print("Sandbox mode: creating a test token for 1.00 MAD; no charge.")
        payload = urlencode({
            "pri_key": private_key,
            "order_id": "tijarago-key-check-" + uuid.uuid4().hex,
            "amount": 100,
            "currency": "MAD",
        }).encode("ascii")
        request = Request(SANDBOX_ENDPOINT, data=payload, method="POST", headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        })
    else:
        print("Live mode: read-only account lookup; no payment is created.")
        credential = base64.b64encode((private_key + ":").encode()).decode("ascii")
        request = Request(ENDPOINT, method="GET", headers={"Accept": "application/json"})
        request.add_unredirected_header("Authorization", "Basic " + credential)
    # Verify TLS certificates; bypass environment proxy settings intentionally.
    opener = build_opener(
        ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), NoRedirect()
    )
    try:
        with opener.open(request, timeout=15) as response:
            if response.status not in ((200, 201) if sandbox else (200,)):
                print("INCONCLUSIVE: unexpected HTTP status.")
                return 2
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            print("INCONCLUSIVE: response exceeded the size limit.")
            return 2
        account = json.loads(body)
    except HTTPError as error:
        status = error.code
        error.close()
        if status == 401:
            print("REJECTED: YouCanPay did not accept this credential.")
            return 1
        if status == 403:
            print("BLOCKED: provider denied access; review account permissions/status.")
            return 1
        if status in (400, 422):
            print("INCONCLUSIVE: provider rejected the request (HTTP", status, ").")
            print("This alone does not establish whether the private key is invalid.")
            return 2
        print("INCONCLUSIVE: HTTP", status, "(unexpected response or provider error).")
        return 2
    except (URLError, TimeoutError, socket.timeout, OSError):
        # Do not print exception details, request headers, or raw responses.
        print("INCONCLUSIVE: network, TLS, or timeout error.")
        return 2
    except (ValueError, UnicodeError):
        print("INCONCLUSIVE: response was not valid JSON.")
        return 2

    if sandbox:
        token = account.get("token") if isinstance(account, dict) else None
        token_id = token.get("id") if isinstance(token, dict) else token
        if not isinstance(token_id, str) or not token_id.strip():
            print("INCONCLUSIVE: no usable sandbox token in the response.")
            return 2
        print("ACCEPTED: YouCanPay created a sandbox token with this private key.")
        print("No card was submitted and no payment was completed.")
        print("This does not validate a public key or live-account readiness.")
        return 0

    if not isinstance(account, dict) or account.get("object") != "v2.account" or not (
        isinstance(account.get("id"), str) and account["id"]
    ):
        print("INCONCLUSIVE: unexpected account response format.")
        return 2

    print("ACCEPTED: YouCanPay authenticated the private key.")
    # Print a small allowlist; never print the full response or account balance.
    for field in ("status", "identity_verified", "live_mode_enabled"):
        print(field + ":", json.dumps(account.get(field), ensure_ascii=True))
    if account.get("status") == "active" and account.get("identity_verified") is True \
            and account.get("live_mode_enabled") is True:
        print("Account reports active, verified, and live-enabled.")
    else:
        print("Key accepted, but account readiness needs attention.")
    print("This does not validate a public key or guarantee checkout success.")
    return 0


def main():
    print("YouCanPay key check. No card will be charged.")
    print("A pri_sandbox_ key uses sandbox tokenization; other keys use live account lookup.")
    try:
        # Refuse getpass's visible-input fallback in unsupported terminals.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            private_key = getpass.getpass("Private key (hidden): ")
        return check_key(private_key)
    except getpass.GetPassWarning:
        print("Use a local interactive terminal supporting hidden input.")
        return 2
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
