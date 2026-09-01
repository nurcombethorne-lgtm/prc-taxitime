"""Upload a validated submission to the team bucket.

Runs the row validator first and refuses to upload if it fails.

Usage:
    uv run scripts/upload_submission.py submissions/resilient-kiwi_v1.parquet
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from s3util import TEAM_BUCKET, client


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: upload_submission.py <file.parquet>")
    sub = Path(sys.argv[1])

    check = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "validate_submission.py"), str(sub)],
    )
    if check.returncode != 0:
        raise SystemExit("validator failed — not uploading")

    s3 = client()
    s3.upload_file(str(sub), TEAM_BUCKET, sub.name)
    print(f"uploaded {sub.name} to {TEAM_BUCKET}")
    print("A result file should appear in the bucket shortly; check with:")
    print("  uv run scripts/fetch_data.py --discover")


if __name__ == "__main__":
    main()
