"""Shared S3/MinIO helpers for the PRC Data Challenge 2026.

Credentials come from `.env` in the repo root (see `.env.example`).
"""

from __future__ import annotations

import os
from pathlib import Path

import boto3
from botocore.config import Config
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

load_dotenv(REPO_ROOT / ".env")

ENDPOINT = os.environ.get("PRC_S3_ENDPOINT", "https://s3.opensky-network.org")
TEAM_NAME = os.environ.get("PRC_TEAM_NAME", "resilient-kiwi")
TEAM_BUCKET = os.environ.get("PRC_TEAM_BUCKET", "prc-2026-resilient-kiwi")
# Final phase (announced 8 Oct 2026): one blind submission on a four-month
# ranking set. PRC_FINAL=1 points the whole pipeline at the final files.
FINAL = os.environ.get("PRC_FINAL", "") not in ("", "0")
RANKING_FILE = DATA_DIR / ("final_ranking.parquet" if FINAL else "ranking.parquet")
SUBMITTING_FILE = DATA_DIR / ("final_submitting.parquet" if FINAL else "submitting.parquet")
SUBMISSION_TAG = "final" if FINAL else "v"


def client():
    access = os.environ.get("PRC_S3_ACCESS_KEY")
    secret = os.environ.get("PRC_S3_SECRET_KEY")
    if not access or not secret:
        raise SystemExit(
            "Missing PRC_S3_ACCESS_KEY / PRC_S3_SECRET_KEY.\n"
            "Copy .env.example to .env and fill in a MinIO access key "
            "(create one at https://s3.opensky-network.org after SSO login)."
        )
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        aws_session_token=os.environ.get("PRC_S3_SESSION_TOKEN") or None,
        config=Config(signature_version="s3v4", retries={"max_attempts": 5}),
    )


def list_bucket(s3, bucket: str, prefix: str = ""):
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        yield from page.get("Contents", [])
