"""Get temporary S3 credentials without the MinIO web console.

The console at https://s3.opensky-network.org:9443 is unreachable (the
port is filtered at OpenSky's end), so we reproduce what it does over
port 443:

  1. OAuth2 *device authorization* flow against OpenSky's Keycloak, using
     client_id=minio-client (the client MinIO trusts). You approve in a
     browser tab on auth.opensky-network.org -- NOT the broken :9443
     console -- so no console access is needed.
  2. exchange the resulting OIDC token at the S3 endpoint's STS API for
     temporary S3 credentials (AssumeRoleWithWebIdentity).

The token must be minted for minio-client, otherwise MinIO rejects it
with "azp claim invalid" (which is what a trino-client token gets).

Writes PRC_S3_ACCESS_KEY / PRC_S3_SECRET_KEY / PRC_S3_SESSION_TOKEN into
`.env` (gitignored). Nothing is sent anywhere except OpenSky's own
servers.

Run it yourself:
    uv run scripts/sts_login.py
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

REALM = "https://auth.opensky-network.org/auth/realms/opensky-network"
DEVICE_URL = f"{REALM}/protocol/openid-connect/auth/device"
TOKEN_URL = f"{REALM}/protocol/openid-connect/token"
STS_ENDPOINT = "https://s3.opensky-network.org/"

# Clients to try for the device flow, best guess first. minio-client is
# the console's own client (so its token has the azp MinIO expects).
CLIENT_IDS = ("minio-client", "minio", "trino-client")
DURATION_SEC = 12 * 3600


def post(url: str, data: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def start_device_flow(client_id: str) -> dict | None:
    status, body = post(DEVICE_URL, {"client_id": client_id, "scope": "openid"})
    if status != 200:
        print(f"  device auth ({client_id}): HTTP {status} {body[:140]}")
        return None
    return json.loads(body)


def poll_for_token(client_id: str, device: dict) -> str | None:
    interval = device.get("interval", 5)
    deadline = time.time() + device.get("expires_in", 600)
    while time.time() < deadline:
        time.sleep(interval)
        status, body = post(
            TOKEN_URL,
            {
                "client_id": client_id,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device["device_code"],
            },
        )
        if status == 200:
            return json.loads(body)["access_token"]
        err = json.loads(body).get("error", "")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        print(f"  token poll: {err or body[:140]}")
        return None
    print("  device code expired before approval")
    return None


def sts_exchange(token: str) -> dict[str, str] | None:
    status, body = post(
        STS_ENDPOINT,
        {
            "Action": "AssumeRoleWithWebIdentity",
            "Version": "2011-06-15",
            "WebIdentityToken": token,
            "DurationSeconds": str(DURATION_SEC),
        },
    )
    if status != 200:
        print(f"  sts: HTTP {status} {body[:220]}")
        return None
    creds = {}
    for tag, key in (
        ("AccessKeyId", "PRC_S3_ACCESS_KEY"),
        ("SecretAccessKey", "PRC_S3_SECRET_KEY"),
        ("SessionToken", "PRC_S3_SESSION_TOKEN"),
    ):
        m = re.search(rf"<{tag}>([^<]+)</{tag}>", body)
        if not m:
            print(f"  sts: no <{tag}> in response")
            return None
        creds[key] = m.group(1)
    return creds


def write_env(creds: dict[str, str]) -> None:
    if ENV_FILE.exists():
        lines = [
            ln for ln in ENV_FILE.read_text().splitlines()
            if not ln.startswith(tuple(creds))
        ]
    else:
        lines = [
            "PRC_S3_ENDPOINT=https://s3.opensky-network.org",
            "PRC_TEAM_NAME=resilient-kiwi",
            "PRC_TEAM_BUCKET=prc-2026-resilient-kiwi",
        ]
    lines += [f"{k}={v}" for k, v in creds.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n")


def main() -> None:
    for client_id in CLIENT_IDS:
        print(f"\n=== device login with client_id={client_id} ===")
        device = start_device_flow(client_id)
        if not device:
            continue

        url = device.get("verification_uri_complete") or device["verification_uri"]
        print("\n  Open this URL in a browser and approve the login:")
        print(f"    {url}")
        if not device.get("verification_uri_complete"):
            print(f"  and enter code: {device['user_code']}")
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        print("\n  Waiting for you to approve in the browser ...")

        token = poll_for_token(client_id, device)
        if not token:
            continue
        creds = sts_exchange(token)
        if creds:
            write_env(creds)
            print(f"\nTemporary credentials written to {ENV_FILE}")
            print(f"Valid up to {DURATION_SEC // 3600}h (server may cap lower). "
                  "Re-run this script to renew.")
            print("Next: uv run scripts/fetch_data.py --discover")
            return
        print(f"  {client_id} token was minted but MinIO rejected it; trying next client.")

    sys.exit(
        "\nAll clients failed. If every attempt hit the azp/STS error, ask on "
        "the challenge Discord which Keycloak client MinIO trusts, or email "
        "challenge@opensky-network.org — the :9443 console is unreachable."
    )


if __name__ == "__main__":
    main()
