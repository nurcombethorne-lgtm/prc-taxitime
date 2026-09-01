"""Get temporary S3 credentials without the MinIO web console.

The console at https://s3.opensky-network.org:9443 does SSO via OpenSky's
Keycloak and then calls MinIO's STS AssumeRoleWithWebIdentity API. When
the console is unreachable, this script performs the same two steps
directly over port 443:

  1. password grant against Keycloak (auth.opensky-network.org) to get
     an OIDC access token,
  2. exchange it at the S3 endpoint for temporary credentials,

then writes PRC_S3_ACCESS_KEY / PRC_S3_SECRET_KEY / PRC_S3_SESSION_TOKEN
into `.env` (gitignored). Credentials are prompted interactively and sent
only to auth.opensky-network.org.

Run it yourself:
    uv run scripts/sts_login.py
"""

from __future__ import annotations

import getpass
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

KEYCLOAK_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/"
    "opensky-network/protocol/openid-connect/token"
)
STS_ENDPOINT = "https://s3.opensky-network.org/"
# trino-client is the only public client on this realm that allows the
# password grant (probed 2026-09-01; the console's own minio-client
# rejects it with unauthorized_client).
CLIENT_IDS = ("trino-client",)
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


def get_oidc_token(client_id: str, username: str, password: str) -> str | None:
    status, body = post(
        KEYCLOAK_TOKEN_URL,
        {
            "client_id": client_id,
            "grant_type": "password",
            "username": username,
            "password": password,
        },
    )
    if status != 200:
        print(f"  keycloak ({client_id}): HTTP {status} {body[:120]}")
        return None
    m = re.search(r'"access_token"\s*:\s*"([^"]+)"', body)
    return m.group(1) if m else None


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
        print(f"  sts: HTTP {status} {body[:200]}")
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
    lines: list[str] = []
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
    print("OpenSky Network login (sent only to auth.opensky-network.org)")
    username = input("  username: ").strip()
    password = getpass.getpass("  password: ")

    for client_id in CLIENT_IDS:
        print(f"trying client_id={client_id} ...")
        token = get_oidc_token(client_id, username, password)
        if not token:
            continue
        creds = sts_exchange(token)
        if creds:
            write_env(creds)
            print(f"\nTemporary credentials written to {ENV_FILE}")
            print(f"Valid for up to {DURATION_SEC // 3600}h "
                  "(server may cap this lower). Re-run this script to renew.")
            print("Next: uv run scripts/fetch_data.py --discover")
            return
    sys.exit(
        "\nAll attempts failed. The MinIO STS API may be disabled for "
        "direct use, or none of the tried client_ids allow the password "
        "grant. Fall back to the web console when it is reachable, or "
        "email challenge@opensky-network.org."
    )


if __name__ == "__main__":
    main()
