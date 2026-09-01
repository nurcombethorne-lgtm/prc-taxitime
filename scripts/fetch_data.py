"""Download the competition datasets from OpenSky MinIO into ./data/.

Usage:
    uv run scripts/fetch_data.py --discover     # list visible buckets/keys
    uv run scripts/fetch_data.py                # download from the data bucket
    uv run scripts/fetch_data.py --bucket NAME  # override the source bucket

The competition bucket name is taken from --bucket, else the
PRC_DATA_BUCKET env var, else discovery mode is suggested.
Existing files with matching size are skipped, so reruns are cheap.
"""

from __future__ import annotations

import argparse
import os
import sys

from s3util import DATA_DIR, client, list_bucket


def discover(s3) -> None:
    try:
        buckets = s3.list_buckets().get("Buckets", [])
    except Exception as exc:  # noqa: BLE001
        print(f"list_buckets failed ({exc}); your key may be scoped to specific buckets.")
        buckets = []
    for b in buckets:
        name = b["Name"]
        print(f"bucket: {name}")
        try:
            for i, obj in enumerate(list_bucket(s3, name)):
                print(f"  {obj['Key']}  ({obj['Size']:,} bytes)")
                if i >= 49:
                    print("  ... (truncated at 50 keys)")
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"  (cannot list: {exc})")


def download(s3, bucket: str) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    objects = list(list_bucket(s3, bucket))
    if not objects:
        raise SystemExit(f"No objects visible in bucket '{bucket}'.")
    total = sum(o["Size"] for o in objects)
    print(f"{len(objects)} objects, {total / 1e6:,.1f} MB total in '{bucket}'")
    for obj in objects:
        key, size = obj["Key"], obj["Size"]
        dest = DATA_DIR / key
        if dest.exists() and dest.stat().st_size == size:
            print(f"  skip {key} (already downloaded)")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"  get  {key} ({size / 1e6:,.1f} MB)")
        s3.download_file(bucket, key, str(dest))
    print("done.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true", help="list visible buckets and keys")
    ap.add_argument("--bucket", default=os.environ.get("PRC_DATA_BUCKET"))
    args = ap.parse_args()

    s3 = client()
    if args.discover:
        discover(s3)
        return
    if not args.bucket:
        print("No data bucket set. Run with --discover to find it, then either")
        print("pass --bucket NAME or set PRC_DATA_BUCKET in .env.")
        sys.exit(1)
    download(s3, args.bucket)


if __name__ == "__main__":
    main()
