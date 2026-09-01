"""Week-1 baseline: median taxi-out by airport x stand x runway.

Hierarchical fallback at predict time:
  airport x stand x runway  ->  airport x runway  ->  airport  ->  global.

Fit on the twelve 2025 training months; validated on Jan + Jul 2025
(the same months of year the ranking set uses) rather than a random
split, since taxi-out is strongly seasonal.

Column names are resolved at runtime by pattern because the exact _mvt
schema is only known once the data is downloaded; run with --probe to
see what was matched and fix COLUMN_HINTS if needed.

Usage:
    uv run scripts/baseline.py --probe
    uv run scripts/baseline.py            # fit, validate, write submission
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from s3util import DATA_DIR, TEAM_NAME

SUBMISSIONS = Path(__file__).resolve().parent.parent / "submissions"

# Pattern -> role. First matching column (case-insensitive substring) wins.
COLUMN_HINTS = {
    "airport": ("ADEP", "AIRPORT", "APT", "ICAO"),
    "stand": ("STAND", "PARK", "GATE"),
    "runway": ("RWY", "RUNWAY"),
}
TARGET = "TAXITIME_SEC_mvt"
PHASE_FILTER = "PHASE = 'DEP'"


def training_files() -> list[Path]:
    files = sorted(DATA_DIR.glob("training_*.parquet"))
    if not files:
        raise SystemExit("no training files in data/ — run fetch_data.py first")
    return files


def resolve_columns(con, sample: Path) -> dict[str, str]:
    cols = [r[0] for r in con.sql(
        f"DESCRIBE SELECT * FROM read_parquet('{sample}')").fetchall()]
    resolved = {}
    for role, hints in COLUMN_HINTS.items():
        match = next(
            (c for h in hints for c in cols
             if h in c.upper() and c.endswith("_mvt")),
            None,
        ) or next((c for h in hints for c in cols if h in c.upper()), None)
        if match is None:
            raise SystemExit(f"no column found for role '{role}' — adjust COLUMN_HINTS.\nColumns: {cols}")
        resolved[role] = match
    return resolved


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="show resolved columns and exit")
    args = ap.parse_args()

    con = duckdb.connect()
    files = training_files()
    cols = resolve_columns(con, files[0])
    print(f"resolved columns: {cols}")
    if args.probe:
        return

    apt, stand, rwy = cols["airport"], cols["stand"], cols["runway"]
    train_glob = f"{DATA_DIR}/training_*.parquet"

    # Validation months mirror the ranking months (Jan + Jul).
    val_files = [f for f in files if "2025-01" in f.name or "2025-07" in f.name]
    fit_files = [f for f in files if f not in val_files]
    fit_list = ", ".join(f"'{f}'" for f in fit_files)
    val_list = ", ".join(f"'{f}'" for f in val_files)

    con.sql(f"""
        CREATE TEMP TABLE fit AS
        SELECT "{apt}" AS apt, "{stand}" AS stand, "{rwy}" AS rwy,
               {TARGET} AS y
        FROM read_parquet([{fit_list}])
        WHERE {PHASE_FILTER} AND {TARGET} IS NOT NULL AND {TARGET} > 0
    """)
    con.sql("""
        CREATE TEMP TABLE m_asr AS SELECT apt, stand, rwy, median(y) AS p, count(*) AS n
            FROM fit GROUP BY 1,2,3 HAVING count(*) >= 10;
    """)
    con.sql("CREATE TEMP TABLE m_ar AS SELECT apt, rwy, median(y) AS p FROM fit GROUP BY 1,2")
    con.sql("CREATE TEMP TABLE m_a AS SELECT apt, median(y) AS p FROM fit GROUP BY 1")
    g = con.sql("SELECT median(y) FROM fit").fetchone()[0]

    predict = f"""
        SELECT t.*, coalesce(m1.p, m2.p, m3.p, {g}) AS pred
        FROM src t
        LEFT JOIN m_asr m1 ON t.apt = m1.apt AND t.stand = m1.stand AND t.rwy = m1.rwy
        LEFT JOIN m_ar  m2 ON t.apt = m2.apt AND t.rwy = m2.rwy
        LEFT JOIN m_a   m3 ON t.apt = m3.apt
    """

    # Validate on Jan + Jul 2025
    con.sql(f"""
        CREATE TEMP TABLE src AS
        SELECT "{apt}" AS apt, "{stand}" AS stand, "{rwy}" AS rwy, {TARGET} AS y
        FROM read_parquet([{val_list}])
        WHERE {PHASE_FILTER} AND {TARGET} IS NOT NULL AND {TARGET} > 0
    """)
    rmse, n = con.sql(
        f"SELECT sqrt(avg((pred - y)^2)), count(*) FROM ({predict})").fetchone()
    print(f"validation (Jan+Jul 2025): n={n:,} RMSE={rmse:.1f}s")
    per_apt = con.sql(
        f"SELECT apt, count(*), sqrt(avg((pred - y)^2)) FROM ({predict}) GROUP BY 1 ORDER BY 1"
    ).fetchall()
    for a, cnt, r in per_apt:
        print(f"  {a}: n={cnt:,} RMSE={r:.1f}s")

    # Refit on ALL 2025 months, then predict the ranking departures.
    ranking = DATA_DIR / "ranking.parquet"
    template = DATA_DIR / "submitting.parquet"
    if not ranking.exists() or not template.exists():
        print("ranking/submitting parquet missing — skipping submission build")
        return

    con.sql("DELETE FROM fit")
    con.sql(f"""
        INSERT INTO fit
        SELECT "{apt}", "{stand}", "{rwy}", {TARGET}
        FROM read_parquet('{train_glob}')
        WHERE {PHASE_FILTER} AND {TARGET} IS NOT NULL AND {TARGET} > 0
    """)
    for tbl in ("m_asr", "m_ar", "m_a"):
        con.sql(f"DELETE FROM {tbl}")
    con.sql("INSERT INTO m_asr SELECT apt, stand, rwy, median(y), count(*) FROM fit GROUP BY 1,2,3 HAVING count(*) >= 10")
    con.sql("INSERT INTO m_ar SELECT apt, rwy, median(y) FROM fit GROUP BY 1,2")
    con.sql("INSERT INTO m_a SELECT apt, median(y) FROM fit GROUP BY 1")
    g = con.sql("SELECT median(y) FROM fit").fetchone()[0]

    con.sql("DROP TABLE src")
    con.sql(f"""
        CREATE TEMP TABLE src AS
        SELECT MVT_ID_mvt, "{apt}" AS apt, "{stand}" AS stand, "{rwy}" AS rwy
        FROM read_parquet('{ranking}')
        WHERE {PHASE_FILTER}
    """)
    SUBMISSIONS.mkdir(exist_ok=True)
    existing = sorted(SUBMISSIONS.glob(f"{TEAM_NAME}_v*.parquet"))
    version = len(existing) + 1
    out = SUBMISSIONS / f"{TEAM_NAME}_v{version}.parquet"
    con.sql(f"""
        COPY (
            SELECT s.MVT_ID_mvt,
                   coalesce(p.pred, {g}) AS TAXITIME_SEC_mvt
            FROM read_parquet('{template}') s
            LEFT JOIN ({predict.replace('t.*', 't.MVT_ID_mvt, t.apt, t.rwy')}) p
              USING (MVT_ID_mvt)
        ) TO '{out}' (FORMAT PARQUET)
    """)
    print(f"wrote {out}")
    print("Now run: uv run scripts/validate_submission.py", out)


if __name__ == "__main__":
    main()
