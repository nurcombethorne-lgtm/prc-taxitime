"""Validate a submission file against submitting.parquet before upload.

The organisers' script errors on any MVT_ID_mvt mismatch, so this checks:
  - exactly the columns MVT_ID_mvt and TAXITIME_SEC_mvt
  - the exact same set of MVT_ID_mvt values (no missing, extra, dupes)
  - no null / NaN / negative predictions
  - filename matches resilient-kiwi_v<N>.parquet (or _final.parquet for the final phase)

Usage:
    uv run scripts/validate_submission.py submissions/resilient-kiwi_v1.parquet
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import duckdb

from s3util import DATA_DIR, TEAM_NAME, SUBMITTING_FILE

TEMPLATE = SUBMITTING_FILE


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: validate_submission.py <file.parquet>")
    sub = Path(sys.argv[1])
    if not sub.exists():
        fail(f"{sub} does not exist")
    if not TEMPLATE.exists():
        fail(f"{TEMPLATE} not found — run fetch_data.py first")

    if not re.fullmatch(rf"{re.escape(TEAM_NAME)}_(v\d+|final)\.parquet", sub.name):
        fail(f"filename must be {TEAM_NAME}_v<N>.parquet or {TEAM_NAME}_final.parquet, got {sub.name}")

    con = duckdb.connect()
    cols = [r[0] for r in con.sql(f"DESCRIBE SELECT * FROM read_parquet('{sub}')").fetchall()]
    if set(cols) != {"MVT_ID_mvt", "TAXITIME_SEC_mvt"}:
        fail(f"columns must be exactly MVT_ID_mvt, TAXITIME_SEC_mvt; got {cols}")

    n_dupes = con.sql(
        f"SELECT count(*) - count(DISTINCT MVT_ID_mvt) FROM read_parquet('{sub}')"
    ).fetchone()[0]
    if n_dupes:
        fail(f"{n_dupes} duplicate MVT_ID_mvt values")

    missing, extra = con.sql(f"""
        SELECT
          (SELECT count(*) FROM read_parquet('{TEMPLATE}') t
            WHERE t.MVT_ID_mvt NOT IN (SELECT MVT_ID_mvt FROM read_parquet('{sub}'))),
          (SELECT count(*) FROM read_parquet('{sub}') s
            WHERE s.MVT_ID_mvt NOT IN (SELECT MVT_ID_mvt FROM read_parquet('{TEMPLATE}')))
    """).fetchone()
    if missing:
        fail(f"{missing} template rows missing from submission")
    if extra:
        fail(f"{extra} extra rows not in template")

    n_bad = con.sql(f"""
        SELECT count(*) FROM read_parquet('{sub}')
        WHERE TAXITIME_SEC_mvt IS NULL OR isnan(TAXITIME_SEC_mvt::DOUBLE)
           OR TAXITIME_SEC_mvt < 0
    """).fetchone()[0]
    if n_bad:
        fail(f"{n_bad} null/NaN/negative predictions")

    n, lo, med, hi = con.sql(f"""
        SELECT count(*), min(TAXITIME_SEC_mvt), median(TAXITIME_SEC_mvt),
               max(TAXITIME_SEC_mvt)
        FROM read_parquet('{sub}')
    """).fetchone()
    print(f"OK: {n:,} rows, taxi-out min={lo:.0f}s median={med:.0f}s max={hi:.0f}s")
    print("Safe to upload.")


if __name__ == "__main__":
    main()
