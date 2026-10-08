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

from s3util import DATA_DIR, RANKING_FILE

DB = DATA_DIR / "features.duckdb"
SEG = DATA_DIR / "adsb" / "segments"


def main() -> None:
    files = sorted(SEG.glob("*.parquet"))
    days = [f.stem for f in files]
    print(f"{len(files)} segment days", flush=True)
    con = duckdb.connect(str(DB))
    # Share the machine with the trace pull: cap memory so DuckDB does not
    # thrash swap against nine extraction workers on a 16 GB laptop.
    con.sql("SET memory_limit = '5GB'")
    con.sql("SET threads = 6")
    con.sql(f"SET temp_directory = '{DATA_DIR / 'duckdb_tmp'}'")
    import time as _t
    _t0 = _t.time()

    def stage(name: str) -> None:
        print(f"  [{_t.time() - _t0:6.0f}s] {name}", flush=True)
    stage("read segments")
    con.sql(f"CREATE OR REPLACE TEMP TABLE seg AS SELECT *, upper(trim(callsign)) cs FROM read_parquet({[str(f) for f in files]!r}, union_by_name=true)")
    for col, typ in (("path_m", "DOUBLE"), ("stopped_s", "DOUBLE"), ("n_stops", "BIGINT"),
                     ("max_gs", "DOUBLE"), ("actype", "VARCHAR"), ("reg", "VARCHAR")):
        con.sql(f"ALTER TABLE seg ADD COLUMN IF NOT EXISTS {col} {typ}")   # old-schema days
    con.sql(f"CREATE OR REPLACE TEMP TABLE covered AS SELECT unnest({days!r}::DATE[]) AS d")
    stage("read departures")
    con.sql(f"""
        CREATE OR REPLACE TEMP TABLE dep0 AS
        SELECT MVT_ID_mvt mvt_id, ADEP_mvt apt, upper(trim(CALLSIGN_flt)) cs,
               upper(trim(FLIGHT_mvt)) flt, STAND_mvt stand,
               epoch(MVT_TIME_UTC_mvt) t_off, epoch(AOBT_3_flt) t_aobt,
               MVT_TIME_UTC_mvt::DATE d
        FROM read_parquet(['{DATA_DIR}/training_*.parquet', '{RANKING_FILE}'])
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
    #
    # Two hash joins instead of one OR-join (which forced a nested loop and
    # took hours at 400+ days): exact callsign keys, then airline prefix
    # keyed on a 10-minute lift-off bucket (+/-1 bucket covers the 90 s
    # window). Same rows as the single-join form.
    stage("match: exact keys")
    con.sql("""
        CREATE OR REPLACE TEMP TABLE seg_k AS
        SELECT * FROM seg WHERE cs IS NOT NULL
    """)
    # Three equi-joins (one per key) so DuckDB hash-joins each; an IN-list
    # over columns is not an equi-join and degenerates to a nested loop.
    con.sql("""
        CREATE OR REPLACE TEMP TABLE cand_exact AS
        SELECT mvt_id, hex, apt, callsign, cs, t_first_ground, t_first_moving, t_last_ground, t_first_air,
               n_ground, lat0, lon0, gs_first, stale_first, path_m, stopped_s, n_stops, max_gs, actype, reg,
               min(kind) AS kind, min(dist) AS dist
        FROM (
            SELECT d.mvt_id, d.t_off, s.*, 1 AS kind FROM dep d JOIN seg_k s ON s.apt = d.apt AND s.cs = d.cs
            UNION ALL
            SELECT d.mvt_id, d.t_off, s.*, 2 FROM dep d JOIN seg_k s ON s.apt = d.apt AND s.cs = d.flt
            UNION ALL
            SELECT d.mvt_id, d.t_off, s.*, 3 FROM dep d JOIN seg_k s ON s.apt = d.apt AND s.cs = d.cs_guess
        ) j
        , LATERAL (SELECT coalesce(abs(j.t_first_air - j.t_off), abs(j.t_last_ground - j.t_off) + 60) AS dist) x
        WHERE (j.t_first_air IS NOT NULL AND abs(j.t_first_air - j.t_off) <= 180)
           OR (j.t_first_air IS NULL AND j.t_off - j.t_last_ground BETWEEN 0 AND 360)
        GROUP BY ALL
    """)
    stage("match: airline prefix")
    con.sql("""
        CREATE OR REPLACE TEMP TABLE seg_air AS
        SELECT *, regexp_extract(cs, '^([A-Z]{3})', 1) AS ic,
               (t_first_air // 600)::BIGINT AS bkt
        FROM seg_k WHERE t_first_air IS NOT NULL
    """)
    con.sql("""
        CREATE OR REPLACE TEMP TABLE cand_prefix AS
        SELECT DISTINCT d.mvt_id, s.* EXCLUDE (ic, bkt), 4 AS kind,
               abs(s.t_first_air - d.t_off) AS dist
        FROM (SELECT d.*, (d.t_off // 600)::BIGINT + u.o AS bkt
              FROM dep d, (SELECT unnest([-1, 0, 1]) AS o) u
              WHERE d.ic IS NOT NULL AND d.ic <> '') d
        JOIN seg_air s ON s.apt = d.apt AND s.ic = d.ic AND s.bkt = d.bkt
        WHERE abs(s.t_first_air - d.t_off) <= 90
    """)
    con.sql("""
        CREATE OR REPLACE TEMP TABLE cand AS
        SELECT * FROM cand_exact
        UNION ALL
        SELECT * FROM cand_prefix p
        WHERE NOT EXISTS (SELECT 1 FROM cand_exact e WHERE e.mvt_id = p.mvt_id
                          AND e.hex = p.hex AND e.t_first_ground = p.t_first_ground)
    """)
    stage("pick best candidate")
    con.sql("""
        CREATE OR REPLACE TEMP TABLE m AS
        SELECT * FROM (
            SELECT c.*, row_number() OVER (PARTITION BY mvt_id ORDER BY kind, dist) rn,
                   count(*) FILTER (WHERE kind = 4) OVER (PARTITION BY mvt_id) n4
            FROM cand c
        ) WHERE rn = 1 AND (kind < 4 OR n4 = 1)
    """)
    # Stand positions learnt from the traces themselves: the median first
    # position of aircraft that were stationary when first heard, per stand.
    # The distance from that point to where THIS aircraft was first heard
    # measures how much of the taxi the trace missed (at EDDF/LEBL the
    # transponder comes alive mid-taxi, 5-6 min after block).
    stage("stand positions")
    con.sql("""
        CREATE OR REPLACE TEMP TABLE standpos AS
        SELECT d.apt, d.stand, median(m.lat0) slat, median(m.lon0) slon, count(*) n
        FROM m JOIN dep d USING (mvt_id)
        WHERE d.stand IS NOT NULL AND m.kind <= 3 AND coalesce(m.gs_first, 0) < 1
        GROUP BY 1, 2 HAVING count(*) >= 5
    """)
    con.sql("""
        CREATE OR REPLACE TEMP TABLE pos AS
        SELECT m.mvt_id,
               sqrt(pow((m.lat0 - sp.slat) * 111320.0, 2)
                  + pow((m.lon0 - sp.slon) * 111320.0 * cos(radians(m.lat0)), 2)) AS adsb_dist_stand_m
        FROM m JOIN dep d USING (mvt_id)
        JOIN standpos sp ON sp.apt = d.apt AND sp.stand = d.stand
    """)

    # (Trace-derived surface counts, `adsb_surf`, were tested on 25 Sep and
    # were null; the table scaled badly with the number of days and was
    # removed on 29 Sep. The columns stay, as nulls, so the opt-in group
    # still resolves.)
    stage("write adsb table")
    con.sql("""
        CREATE OR REPLACE TABLE adsb AS
        SELECT d.mvt_id,
               NULL::BIGINT AS adsb_taxiing, NULL::BIGINT AS adsb_liftoffs_prev15,
               (c.d IS NOT NULL)::INT              AS adsb_day_covered,
               (m.mvt_id IS NOT NULL)::INT         AS adsb_present,
               m.kind                              AS adsb_match_kind,
               pos.adsb_dist_stand_m,
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
        LEFT JOIN pos ON pos.mvt_id = d.mvt_id
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
