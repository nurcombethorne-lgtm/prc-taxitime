"""Join adsb.lol ground segments onto departures.

Writes an `adsb` table into data/features.duckdb keyed by mvt_id, like
`wx`. Segments come from adsb_pull.py / adsb_extract.py (one parquet per
day in data/adsb/segments/). A departure is matched to the segment at its
airport with the same callsign whose lift-off is within 3 min of the
movement take-off time (fallback: last ground point within 6 min before).

Features are airport-agnostic; the tree learns where the trace is a
witness to block time (EHAM: transponder on at pushback) and where it is
a lagged taxi-start proxy (the rest). `adsb_day_covered` separates "no
trace on a covered day" from "day not processed", which otherwise share
a null.

    uv run scripts/adsb_features.py
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from s3util import DATA_DIR

DB = DATA_DIR / "features.duckdb"
SEG = DATA_DIR / "adsb" / "segments"


def main() -> None:
    files = sorted(SEG.glob("*.parquet"))
    days = [f.stem for f in files]
    print(f"{len(files)} segment days")
    con = duckdb.connect(str(DB))
    con.sql(f"CREATE OR REPLACE TEMP TABLE seg AS SELECT *, upper(trim(callsign)) cs FROM read_parquet({[str(f) for f in files]!r})")
    con.sql(f"CREATE OR REPLACE TEMP TABLE covered AS SELECT unnest({days!r}::DATE[]) AS d")
    con.sql(f"""
        CREATE OR REPLACE TEMP TABLE dep AS
        SELECT MVT_ID_mvt mvt_id, ADEP_mvt apt, upper(trim(CALLSIGN_flt)) cs,
               epoch(MVT_TIME_UTC_mvt) t_off, epoch(AOBT_3_flt) t_aobt,
               MVT_TIME_UTC_mvt::DATE d
        FROM read_parquet(['{DATA_DIR}/training_*.parquet', '{DATA_DIR}/ranking.parquet'])
        WHERE PHASE_mvt = 'DEP'
    """)
    con.sql("""
        CREATE OR REPLACE TEMP TABLE m AS
        SELECT * FROM (
            SELECT d.mvt_id, s.t_first_ground, s.t_first_moving, s.t_last_ground, s.t_first_air,
                   s.n_ground, s.gs_first,
                   row_number() OVER (PARTITION BY d.mvt_id ORDER BY
                       coalesce(abs(s.t_first_air - d.t_off), abs(s.t_last_ground - d.t_off) + 60)) rn
            FROM dep d JOIN seg s ON s.apt = d.apt AND s.cs = d.cs
            WHERE (s.t_first_air IS NOT NULL AND abs(s.t_first_air - d.t_off) <= 180)
               OR (s.t_first_air IS NULL AND d.t_off - s.t_last_ground BETWEEN 0 AND 360)
        ) WHERE rn = 1
    """)
    con.sql("""
        CREATE OR REPLACE TABLE adsb AS
        SELECT d.mvt_id,
               (c.d IS NOT NULL)::INT              AS adsb_day_covered,
               (m.mvt_id IS NOT NULL)::INT         AS adsb_present,
               d.t_off - m.t_first_moving          AS adsb_taxi_moving,
               d.t_off - m.t_first_ground          AS adsb_taxi_firstobs,
               m.t_first_moving - d.t_aobt         AS adsb_moving_vs_aobt,
               m.t_first_ground - d.t_aobt         AS adsb_firstobs_vs_aobt,
               m.t_first_moving - m.t_first_ground AS adsb_wait_before_moving,
               m.n_ground                          AS adsb_n_ground
        FROM dep d
        LEFT JOIN covered c ON c.d = d.d
        LEFT JOIN m ON m.mvt_id = d.mvt_id
    """)
    print(con.sql("""
        SELECT CASE WHEN mvt_id IN (SELECT mvt_id FROM rank_feat) THEN 'rank' ELSE 'train' END s,
               count(*) n, sum(adsb_day_covered) covered, sum(adsb_present) present,
               round(100.0 * sum(adsb_present) / nullif(sum(adsb_day_covered), 0), 1) pct_of_covered
        FROM adsb GROUP BY 1 ORDER BY 1
    """).df().to_string(index=False))
    con.close()


if __name__ == "__main__":
    main()
