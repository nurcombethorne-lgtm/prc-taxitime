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
    con.sql(f"CREATE OR REPLACE TEMP TABLE seg AS SELECT *, upper(trim(callsign)) cs FROM read_parquet({[str(f) for f in files]!r}, union_by_name=true)")
    con.sql(f"CREATE OR REPLACE TEMP TABLE covered AS SELECT unnest({days!r}::DATE[]) AS d")
    con.sql(f"""
        CREATE OR REPLACE TEMP TABLE dep0 AS
        SELECT MVT_ID_mvt mvt_id, ADEP_mvt apt, upper(trim(CALLSIGN_flt)) cs,
               upper(trim(FLIGHT_mvt)) flt,
               epoch(MVT_TIME_UTC_mvt) t_off, epoch(AOBT_3_flt) t_aobt,
               MVT_TIME_UTC_mvt::DATE d
        FROM read_parquet(['{DATA_DIR}/training_*.parquet', '{DATA_DIR}/ranking.parquet'])
        WHERE PHASE_mvt = 'DEP'
    """)
    # Airline prefix map (flight-number prefix -> ICAO callsign prefix), learnt
    # from rows that have both. Lets departures without an NM record (no
    # CALLSIGN_flt) be matched: those rows are 1.5% of the data and a third of
    # the squared error. Precision measured on rows where the NM callsign is
    # known: guessed callsign 99.9%, airline prefix + lift-off within 90 s
    # 96.7%, lift-off time alone 82.6% (not used).
    con.sql("""
        CREATE OR REPLACE TEMP TABLE pm AS
        SELECT ia, ic FROM (
            SELECT regexp_extract(flt, '^([A-Z0-9]{2}[A-Z]?)[0-9]', 1) ia,
                   regexp_extract(cs, '^([A-Z]{3})', 1) ic, count(*) n,
                   row_number() OVER (PARTITION BY regexp_extract(flt, '^([A-Z0-9]{2}[A-Z]?)[0-9]', 1)
                                      ORDER BY count(*) DESC) rn
            FROM dep0 WHERE flt IS NOT NULL AND cs IS NOT NULL GROUP BY 1, 2
        ) WHERE rn = 1 AND ia <> '' AND ic <> ''
    """)
    con.sql("""
        CREATE OR REPLACE TEMP TABLE dep AS
        SELECT d.*, coalesce(pm.ic, regexp_extract(d.cs, '^([A-Z]{3})', 1),
                             regexp_extract(d.flt, '^([A-Z]{3})', 1)) AS ic,
               pm.ic || regexp_extract(d.flt, '([0-9]+[A-Z]*)$', 1) AS cs_guess
        FROM dep0 d
        LEFT JOIN pm ON pm.ia = regexp_extract(d.flt, '^([A-Z0-9]{2}[A-Z]?)[0-9]', 1)
    """)
    # Cascade, most reliable first. kind: 1 NM callsign, 2 flight number as
    # callsign, 3 guessed callsign, 4 airline prefix + lift-off within 90 s
    # (accepted only when exactly one such trace exists).
    con.sql("""
        CREATE OR REPLACE TEMP TABLE cand AS
        SELECT d.mvt_id, s.*, 
               CASE WHEN s.cs = d.cs THEN 1 WHEN s.cs = d.flt THEN 2
                    WHEN s.cs = d.cs_guess THEN 3 ELSE 4 END AS kind,
               coalesce(abs(s.t_first_air - d.t_off), abs(s.t_last_ground - d.t_off) + 60) AS dist
        FROM dep d JOIN seg s ON s.apt = d.apt
         AND (   (s.cs IN (d.cs, d.flt, d.cs_guess)
                  AND ((s.t_first_air IS NOT NULL AND abs(s.t_first_air - d.t_off) <= 180)
                    OR (s.t_first_air IS NULL AND d.t_off - s.t_last_ground BETWEEN 0 AND 360)))
              OR (d.ic IS NOT NULL AND d.ic <> '' AND starts_with(s.cs, d.ic)
                  AND s.t_first_air IS NOT NULL AND abs(s.t_first_air - d.t_off) <= 90))
    """)
    con.sql("""
        CREATE OR REPLACE TEMP TABLE m AS
        SELECT * FROM (
            SELECT c.*, row_number() OVER (PARTITION BY mvt_id ORDER BY kind, dist) rn,
                   count(*) FILTER (WHERE kind = 4) OVER (PARTITION BY mvt_id) n4
            FROM cand c
        ) WHERE rn = 1 AND (kind < 4 OR n4 = 1)
    """)
    # Surface state from ALL traced aircraft at the airport (not only
    # NM-matched flights): how many are taxiing at the departure's off-block
    # reference time, and how many lifted off in the preceding 15 min.
    # Raw correlation with the matched-lane gap 0.16-0.25 at EDDF/EDDM/LEBL/
    # LSZH/LIRF, essentially orthogonal to dep_queue (25 Sep).
    con.sql("""
        CREATE OR REPLACE TEMP TABLE ref AS
        SELECT MVT_ID_mvt mvt_id, ADEP_mvt apt,
               epoch(coalesce(AOBT_3_flt, SCHED_TIME_UTC_mvt, MVT_TIME_UTC_mvt)) t_ref
        FROM read_parquet(['{DATA_DIR}/training_*.parquet', '{DATA_DIR}/ranking.parquet'])
        WHERE PHASE_mvt = 'DEP'
    """.replace("{DATA_DIR}", str(DATA_DIR)))
    con.sql("""
        CREATE OR REPLACE TEMP TABLE mov AS
        SELECT apt, t_first_moving tm, coalesce(t_first_air, t_last_ground) ta
        FROM seg WHERE t_first_moving IS NOT NULL
    """)
    con.sql("""
        CREATE OR REPLACE TEMP TABLE surf AS
        SELECT r.mvt_id,
               (SELECT count(*) FROM mov s WHERE s.apt = r.apt AND s.tm < r.t_ref AND s.ta > r.t_ref) AS adsb_taxiing,
               (SELECT count(*) FROM mov s WHERE s.apt = r.apt AND s.ta BETWEEN r.t_ref - 900 AND r.t_ref) AS adsb_liftoffs_prev15
        FROM ref r
    """)
    con.sql("""
        CREATE OR REPLACE TABLE adsb AS
        SELECT d.mvt_id,
               sf.adsb_taxiing, sf.adsb_liftoffs_prev15,
               (c.d IS NOT NULL)::INT              AS adsb_day_covered,
               (m.mvt_id IS NOT NULL)::INT         AS adsb_present,
               m.kind                              AS adsb_match_kind,
               d.t_off - m.t_first_moving          AS adsb_taxi_moving,
               d.t_off - m.t_first_ground          AS adsb_taxi_firstobs,
               m.t_first_moving - d.t_aobt         AS adsb_moving_vs_aobt,
               m.t_first_ground - d.t_aobt         AS adsb_firstobs_vs_aobt,
               m.t_first_moving - m.t_first_ground AS adsb_wait_before_moving,
               m.n_ground                          AS adsb_n_ground,
               -- movement along the surface (schema 2 segments only)
               m.path_m                            AS adsb_path_m,
               m.stopped_s                         AS adsb_stopped_s,
               m.n_stops                           AS adsb_n_stops,
               m.max_gs                            AS adsb_max_gs,
               m.gs_first                          AS adsb_gs_first,
               m.path_m / nullif(m.t_first_air - m.t_first_moving, 0) AS adsb_mean_speed
        FROM dep d
        LEFT JOIN covered c ON c.d = d.d
        LEFT JOIN m ON m.mvt_id = d.mvt_id
        LEFT JOIN surf sf ON sf.mvt_id = d.mvt_id
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
