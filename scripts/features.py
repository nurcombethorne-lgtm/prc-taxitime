"""Build congestion / queue features for departures.

The central feature is the **departure queue**: how many aircraft are
already off-block but not yet airborne when this aircraft pushes back.
A longer queue means a longer taxi-out.

Two constraints shape the design:

1. BLOCK_TIME_UTC_mvt is blanked for departures in the ranking set, so
   the queue cannot use actual off-block times. AOBT_3_flt is present on
   ~98.5% of departures in BOTH training and ranking, so it is the
   pushback proxy and every feature is computable identically on both.
2. The ranking set is complete for the airport-months it covers
   (Jan 2026: all ten airports; Jul 2026: EDDF, EGLL, EHAM only), so
   per-airport counts are not deflated by sampling.

Everything is computed with ordered window frames over a single event
stream (O(n log n)) rather than range self-joins, which are quadratic and
do not finish on 2M+ rows.
"""

from __future__ import annotations

import duckdb

from s3util import DATA_DIR

WINDOWS_MIN = (15, 30, 60)


def build(con: duckdb.DuckDBPyConnection, source: str, table: str,
          with_target: bool) -> None:
    tgt = ", TAXITIME_SEC_mvt::DOUBLE AS y" if with_target else ""
    con.sql(f"""
        CREATE OR REPLACE TEMP TABLE mv AS
        SELECT MVT_ID_mvt AS mvt_id, ADEP_mvt AS apt, PHASE_mvt AS phase,
               MVT_TIME_UTC_mvt AS mvt_time, AOBT_3_flt AS aobt,
               SCHED_TIME_UTC_mvt AS sched, EOBT_1_flt AS eobt,
               STAND_mvt AS stand, RUNWAY_mvt AS rwy,
               AIRCRAFT_TYPE_mvt AS actype, WK_TBL_CAT_flt AS wake,
               AIRCRAFT_OPERATOR_flt AS operator,
               MARKET_SEGMENT_flt AS segment, ADES_mvt AS ades,
               (AOBT_3_flt IS NULL) AS unmatched {tgt}
        FROM read_parquet('{source}')
    """)

    # --- departure queue: running balance of +1 pushback / -1 take-off ---
    # Only flights with BOTH timestamps enter the event stream: a take-off
    # whose pushback is missing would contribute a -1 with no matching +1,
    # and that imbalance accumulates into a large negative drift over the
    # year rather than staying a local occupancy count.
    con.sql("""
        CREATE OR REPLACE TEMP TABLE qbase AS
        SELECT mvt_id, apt, aobt, mvt_time FROM mv
        WHERE phase='DEP' AND aobt IS NOT NULL AND mvt_time IS NOT NULL
          AND aobt < mvt_time
    """)
    con.sql("""
        CREATE OR REPLACE TEMP TABLE qtab AS
        SELECT mvt_id, q - 1 AS dep_queue FROM (
            SELECT mvt_id, sum(delta) OVER (
                       PARTITION BY apt ORDER BY t, delta
                       ROWS UNBOUNDED PRECEDING) AS q
            FROM (
                SELECT apt, aobt AS t, 1 AS delta, mvt_id FROM qbase
                UNION ALL
                SELECT apt, mvt_time, -1, NULL FROM qbase
            )
        ) WHERE mvt_id IS NOT NULL
    """)

    # --- recent throughput: one ordered stream, counted over RANGE frames ---
    frames = ",\n".join(
        f"""count(*) FILTER (WHERE kind='TKO') OVER (
                PARTITION BY apt ORDER BY t
                RANGE BETWEEN INTERVAL {w} MINUTE PRECEDING AND CURRENT ROW
            ) AS takeoff_prev{w},
            count(*) FILTER (WHERE kind='LND') OVER (
                PARTITION BY apt ORDER BY t
                RANGE BETWEEN INTERVAL {w} MINUTE PRECEDING AND CURRENT ROW
            ) AS landing_prev{w}"""
        for w in WINDOWS_MIN
    )
    con.sql(f"""
        CREATE OR REPLACE TEMP TABLE thr AS
        SELECT mvt_id, {', '.join(f'takeoff_prev{w}, landing_prev{w}' for w in WINDOWS_MIN)}
        FROM (
            SELECT mvt_id, apt, t, kind, {frames}
            FROM (
                SELECT apt, mvt_time AS t, 'TKO' AS kind, NULL::DOUBLE AS mvt_id
                  FROM mv WHERE phase='DEP' AND mvt_time IS NOT NULL
                UNION ALL
                SELECT apt, mvt_time, 'LND', NULL FROM mv
                 WHERE phase='ARR' AND mvt_time IS NOT NULL
                UNION ALL
                SELECT apt, aobt, 'QRY', mvt_id FROM mv
                 WHERE phase='DEP' AND aobt IS NOT NULL
            )
        ) WHERE kind='QRY'
    """)

    # --- planned demand: scheduled departures in the surrounding hour ---
    con.sql("""
        CREATE OR REPLACE TEMP TABLE sd AS
        SELECT mvt_id, cnt AS sched_dep_60 FROM (
            SELECT mvt_id, count(*) OVER (
                PARTITION BY apt ORDER BY sched
                RANGE BETWEEN INTERVAL 30 MINUTE PRECEDING
                          AND INTERVAL 30 MINUTE FOLLOWING) AS cnt
            FROM mv WHERE phase='DEP' AND sched IS NOT NULL
        )
    """)

    ycol = ", m.y" if with_target else ""
    con.sql(f"""
        CREATE OR REPLACE TABLE {table} AS
        SELECT m.mvt_id, m.apt, m.stand, m.rwy, m.actype, m.wake, m.operator,
               m.segment, m.ades, m.unmatched, m.mvt_time,
               epoch(m.mvt_time - m.sched) AS D,
               epoch(m.mvt_time - m.aobt)  AS recov,
               epoch(m.aobt - m.eobt)      AS aobt_vs_eobt,
               coalesce(q.dep_queue, 0) AS dep_queue,
               {', '.join(f'thr.takeoff_prev{w}, thr.landing_prev{w}' for w in WINDOWS_MIN)},
               sd.sched_dep_60,
               extract(hour FROM m.mvt_time) AS hr,
               extract(dow  FROM m.mvt_time) AS dow,
               extract(month FROM m.mvt_time) AS mon,
               extract(year FROM m.mvt_time) AS yr {ycol}
        FROM mv m
        LEFT JOIN qtab q ON q.mvt_id = m.mvt_id
        LEFT JOIN thr   ON thr.mvt_id = m.mvt_id
        LEFT JOIN sd    ON sd.mvt_id = m.mvt_id
        WHERE m.phase = 'DEP'
    """)
    print(f"  {table}: {con.sql(f'SELECT count(*) FROM {table}').fetchone()[0]:,} departures")


def main() -> None:
    out = DATA_DIR / "features.duckdb"
    out.unlink(missing_ok=True)
    con = duckdb.connect(str(out))
    print("building features ...")
    build(con, f"{DATA_DIR}/training_*.parquet", "train_feat", True)
    build(con, f"{DATA_DIR}/ranking.parquet", "rank_feat", False)
    con.close()
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
