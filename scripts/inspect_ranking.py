"""Resolve the open question: is taxi-out recoverable in ranking.parquet?

For PHASE=DEP rows the organisers blank BLOCK_TIME_UTC_mvt and
TAXITIME_SEC_mvt, but MVT_TIME_UTC_mvt (takeoff) survives. If an actual
off-block time also survives on the flight-list side (AOBT_3_flt or any
AOBT-like column), taxi-out = takeoff - off-block would be recoverable by
subtraction.

This script reports, for ranking.parquet:
  1. the full schema,
  2. every column whose name suggests off-block (AOBT/OBT/BLOCK),
  3. for PHASE=DEP rows: null rates of those columns,
  4. if any candidate is populated, the implied taxi-out distribution.

It also cross-checks one training month, where TAXITIME_SEC_mvt is
present, to see how well MVT_TIME_UTC_mvt - AOBT reproduces the official
taxi time (i.e. whether the subtraction is even the same quantity).

NOTE: the organisers explicitly warn against exploiting the ranking
process. This script only *measures* what is present; before using any
recovered value in a submission, ask on the challenge Discord.
"""

from __future__ import annotations

import sys

import duckdb

from s3util import DATA_DIR

RANKING = DATA_DIR / "ranking.parquet"


def main() -> None:
    if not RANKING.exists():
        raise SystemExit(f"{RANKING} not found — run fetch_data.py first.")

    con = duckdb.connect()
    rel = f"read_parquet('{RANKING}')"

    print("=== schema ===")
    schema = con.sql(f"DESCRIBE SELECT * FROM {rel}").fetchall()
    for name, dtype, *_ in schema:
        print(f"  {name:40s} {dtype}")

    cols = [row[0] for row in schema]
    candidates = [
        c for c in cols
        if any(tok in c.upper() for tok in ("AOBT", "OBT", "BLOCK"))
    ]
    print(f"\n=== off-block candidate columns ===\n  {candidates or 'NONE'}")

    n_dep = con.sql(
        f"SELECT count(*) FROM {rel} WHERE PHASE = 'DEP'"
    ).fetchone()[0]
    print(f"\nPHASE=DEP rows: {n_dep:,}")

    for c in candidates:
        n_filled = con.sql(
            f'SELECT count("{c}") FROM {rel} WHERE PHASE = \'DEP\''
        ).fetchone()[0]
        print(f"  {c:40s} filled on DEP rows: {n_filled:,} ({n_filled / max(n_dep,1):.1%})")

    # Implied taxi-out from any populated timestamp candidate
    ts_candidates = [
        c for c in candidates
        if any("TIMESTAMP" in row[1].upper() or "DATE" in row[1].upper()
               for row in schema if row[0] == c)
    ]
    for c in ts_candidates:
        q = f"""
            SELECT
                count(*) AS n,
                min(epoch(MVT_TIME_UTC_mvt - "{c}")) AS min_s,
                median(epoch(MVT_TIME_UTC_mvt - "{c}")) AS med_s,
                max(epoch(MVT_TIME_UTC_mvt - "{c}")) AS max_s
            FROM {rel}
            WHERE PHASE = 'DEP' AND "{c}" IS NOT NULL
              AND MVT_TIME_UTC_mvt IS NOT NULL
        """
        try:
            n, mn, med, mx = con.sql(q).fetchone()
            print(f"\nimplied taxi-out via {c}: n={n:,} min={mn} median={med} max={mx} (seconds)")
        except Exception as exc:  # noqa: BLE001
            print(f"\n{c}: could not compute implied taxi-out ({exc})")

    # Cross-check against a training month if available
    training = sorted(DATA_DIR.glob("training_2025-*-01_*.parquet"))
    if training and ts_candidates:
        t = training[0]
        print(f"\n=== cross-check on {t.name} ===")
        for c in ts_candidates:
            q = f"""
                SELECT
                    count(*) AS n,
                    median(abs(epoch(MVT_TIME_UTC_mvt - "{c}") - TAXITIME_SEC_mvt)) AS med_abs_err,
                    quantile_cont(abs(epoch(MVT_TIME_UTC_mvt - "{c}") - TAXITIME_SEC_mvt), 0.9) AS p90_abs_err
                FROM read_parquet('{t}')
                WHERE PHASE = 'DEP' AND "{c}" IS NOT NULL
                  AND MVT_TIME_UTC_mvt IS NOT NULL AND TAXITIME_SEC_mvt IS NOT NULL
            """
            try:
                n, med, p90 = con.sql(q).fetchone()
                print(f"  {c}: n={n:,}  |takeoff-{c} - official taxi| median={med}s p90={p90}s")
            except Exception as exc:  # noqa: BLE001
                print(f"  {c}: cross-check failed ({exc})")

    print("\nRemember: confirm on Discord before relying on any recovered value.")


if __name__ == "__main__":
    sys.exit(main())
