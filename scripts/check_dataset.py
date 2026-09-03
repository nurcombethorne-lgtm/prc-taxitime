"""Detect when the organisers re-publish the competition datasets.

The July 2026 ranking data was extracted before some airports had
reported, so `ranking.parquet` and `submitting.parquet` are expected to be
regenerated (organiser statement on Discord, 3 Sep 2026). When that
happens:

  - `submitting.parquet` will gain rows, so every existing submission
    becomes invalid (the validator requires an exact MVT_ID_mvt match);
  - the July airport composition changes, which alters the per-airport
    month masks the validation harness depends on.

So the whole pipeline has to be re-run against the new export. This
script compares the bucket against a stored manifest and says what moved.

    uv run scripts/check_dataset.py           # compare against manifest
    uv run scripts/check_dataset.py --save    # record current state
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from s3util import client, list_bucket

MANIFEST = Path(__file__).resolve().parent.parent / "notes" / "dataset_manifest.json"
DATA_BUCKET = "prc-2026-datasets"


def snapshot(s3) -> dict[str, dict]:
    return {
        o["Key"]: {
            "size": o["Size"],
            "etag": o["ETag"].strip('"'),
            "modified": o["LastModified"].strftime("%Y-%m-%d %H:%M:%S"),
        }
        for o in list_bucket(s3, DATA_BUCKET)
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true", help="record current state")
    args = ap.parse_args()

    now = snapshot(client())

    if args.save or not MANIFEST.exists():
        MANIFEST.parent.mkdir(exist_ok=True)
        MANIFEST.write_text(json.dumps(now, indent=2, sort_keys=True) + "\n")
        print(f"recorded {len(now)} objects to {MANIFEST}")
        return

    old = json.loads(MANIFEST.read_text())
    changed = [k for k in now if k in old and now[k]["etag"] != old[k]["etag"]]
    added = [k for k in now if k not in old]
    removed = [k for k in old if k not in now]

    if not (changed or added or removed):
        print(f"no change — {len(now)} objects, still as of "
              f"{next(iter(now.values()))['modified']}")
        return

    print("DATASET CHANGED — the pipeline must be re-run end to end:")
    for k in changed:
        print(f"  modified {k}: {old[k]['size']:,} -> {now[k]['size']:,} bytes")
    for k in added:
        print(f"  added    {k} ({now[k]['size']:,} bytes)")
    for k in removed:
        print(f"  removed  {k}")
    print("\n  1. uv run scripts/fetch_data.py --bucket prc-2026-datasets")
    print("     (delete the stale local copies first: they are size-matched"
          " and would be skipped)")
    print("  2. uv run scripts/features.py")
    print("  3. uv run scripts/model_gbm.py")
    print("  4. validate + upload; then re-record with --save")


if __name__ == "__main__":
    main()
