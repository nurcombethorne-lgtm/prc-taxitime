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
MAX_TURN = 86400  # ignore a stand match older than a day (stale link)


def build(con: duckdb.DuckDBPyConnection, source: str, table: str,
          with_target: bool) -> None:
    # Always carry taxi time into `mv`: for ARRIVALS it is present in the
    # ranking set too and feeds the nowcast. For departures in the ranking
    # set it is NULL (blanked), and it is only emitted as the target when
    # with_target is set.
    tgt = ", TAXITIME_SEC_mvt::DOUBLE AS y"
    con.sql(f"""
        CREATE OR REPLACE TEMP TABLE mv AS
        SELECT MVT_ID_mvt AS mvt_id,
               -- The movement airport differs by phase: for a DEPARTURE it is
               -- ADEP_mvt, but for an ARRIVAL the movement happened at
               -- ADES_mvt (ADEP_mvt is then the *origin*). Using ADEP_mvt for
               -- both buckets arrivals under foreign airports and silently
               -- turns every arrival-derived feature into noise.
               CASE WHEN PHASE_mvt = 'DEP' THEN ADEP_mvt ELSE ADES_mvt END AS apt,
               PHASE_mvt AS phase,
               MVT_TIME_UTC_mvt AS mvt_time, AOBT_3_flt AS aobt,
               SCHED_TIME_UTC_mvt AS sched, EOBT_1_flt AS eobt,
               LOBT_flt AS lobt, IOBT_flt AS iobt,
               BLOCK_TIME_UTC_mvt AS block,
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

    # --- arrival queue: landed but not yet on-block ---
    # Inbound aircraft still on the taxiways compete for the same surface as
    # a departure taxiing out. Only computable once the movement airport is
    # taken from ADES_mvt for arrivals. Same running-balance construction and
    # the same both-timestamps guard as the departure queue.
    con.sql("""
        CREATE OR REPLACE TEMP TABLE abase AS
        SELECT apt, mvt_time, block FROM mv
        WHERE phase='ARR' AND mvt_time IS NOT NULL AND block IS NOT NULL
          AND mvt_time < block
    """)
    con.sql("""
        CREATE OR REPLACE TEMP TABLE atab AS
        SELECT mvt_id, q AS arr_queue FROM (
            SELECT mvt_id, sum(delta) OVER (
                       PARTITION BY apt ORDER BY t, delta
                       ROWS UNBOUNDED PRECEDING) AS q
            FROM (
                SELECT apt, mvt_time AS t, 1 AS delta, NULL::DOUBLE AS mvt_id FROM abase
                UNION ALL
                SELECT apt, block, -1, NULL FROM abase
                UNION ALL
                SELECT apt, aobt, 0, mvt_id FROM mv
                 WHERE phase='DEP' AND aobt IS NOT NULL
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

    # --- contemporaneous conditions ("nowcast") ---
    # Arrival taxi-IN times are NOT blanked in the ranking set, and neither
    # are other departures' take-off/pushback stamps, so the recent state of
    # the airfield is observable at prediction time. Departure taxi-OUT is
    # blanked in ranking and must never be used here.
    con.sql("""
        CREATE OR REPLACE TEMP TABLE nowc AS
        SELECT mvt_id, arr_taxi_mean60, dep_recov_mean60 FROM (
            SELECT mvt_id, kind,
                   avg(val) FILTER (WHERE kind='ARR') OVER w AS arr_taxi_mean60,
                   avg(val) FILTER (WHERE kind='REC') OVER w AS dep_recov_mean60
            FROM (
                SELECT apt, mvt_time AS t, 'ARR' AS kind,
                       y AS val, NULL::DOUBLE AS mvt_id
                  FROM mv WHERE phase='ARR' AND mvt_time IS NOT NULL AND y IS NOT NULL
                UNION ALL
                SELECT apt, mvt_time, 'REC',
                       epoch(mvt_time - aobt), NULL
                  FROM mv WHERE phase='DEP' AND mvt_time IS NOT NULL AND aobt IS NOT NULL
                UNION ALL
                SELECT apt, aobt, 'QRY', NULL, mvt_id
                  FROM mv WHERE phase='DEP' AND aobt IS NOT NULL
            )
            WINDOW w AS (PARTITION BY apt ORDER BY t
                         RANGE BETWEEN INTERVAL 60 MINUTE PRECEDING
                                   AND INTERVAL 1 SECOND PRECEDING)
        ) WHERE kind='QRY'
    """)

    # --- turnaround: link the departure to the arrival that parked here ---
    # Arrivals carry an on-block time (not blanked in the ranking set), so the
    # inbound leg that delivered this aircraft can be recovered as the most
    # recent arrival that went on-block at the same stand before this
    # departure pushes back. Registration is not in the data, so stand+time
    # adjacency is the linkage; a stale match is guarded by MAX_TURN.
    con.sql(f"""
        CREATE OR REPLACE TEMP TABLE turn AS
        SELECT d.mvt_id,
               epoch(d.ref - a.onblock) AS turnaround_sec,
               a.in_delay  AS inbound_delay,
               a.in_taxi   AS inbound_taxi_in
        FROM (SELECT mvt_id, apt, stand, coalesce(aobt, sched, mvt_time) AS ref
              FROM mv WHERE phase='DEP' AND stand IS NOT NULL) d
        ASOF LEFT JOIN (
              SELECT apt, stand, block AS onblock,
                     epoch(mvt_time - sched) AS in_delay,
                     y AS in_taxi
              FROM mv WHERE phase='ARR' AND stand IS NOT NULL AND block IS NOT NULL
        ) a
          ON d.apt = a.apt AND d.stand = a.stand AND d.ref >= a.onblock
    """)
    con.sql(f"""
        CREATE OR REPLACE TEMP TABLE turn AS
        SELECT mvt_id,
               CASE WHEN turnaround_sec BETWEEN 0 AND {MAX_TURN}
                    THEN turnaround_sec END AS turnaround_sec,
               CASE WHEN turnaround_sec BETWEEN 0 AND {MAX_TURN}
                    THEN inbound_delay END AS inbound_delay,
               CASE WHEN turnaround_sec BETWEEN 0 AND {MAX_TURN}
                    THEN inbound_taxi_in END AS inbound_taxi_in
        FROM turn
    """)

    # --- flight-plan revision state, per flight and as an airport nowcast ---
    # For matched flights the residual error is the gap between NM's
    # off-block stamp and the airport's. That gap is clustered within the
    # hour (r 0.12-0.39 with the previous hour's gaps), and the mean of
    # AOBT_3 - EOBT_1 over the previous hour's OTHER departures carries most
    # of that clustering (r 0.20-0.34) while being fully observable in the
    # ranking set. Self is excluded by the 1-second-preceding bound.
    con.sql("""
        CREATE OR REPLACE TEMP TABLE plan AS
        SELECT mvt_id,
               avg(ve)      OVER w60  AS plan_eobt_mean60,
               avg(ve)      OVER w180 AS plan_eobt_mean180,
               avg(vl)      OVER w60  AS plan_lobt_mean60,
               avg(abs(vl)) OVER w60  AS plan_abs_lobt_mean60,
               count(*)     OVER w60  AS plan_n60
        FROM (
            SELECT mvt_id, apt, aobt AS t,
                   epoch(aobt - eobt) AS ve, epoch(aobt - lobt) AS vl
            FROM mv
            WHERE phase='DEP' AND aobt IS NOT NULL AND eobt IS NOT NULL AND lobt IS NOT NULL
        )
        WINDOW
          w60  AS (PARTITION BY apt ORDER BY t RANGE BETWEEN INTERVAL 60 MINUTE PRECEDING
                                                       AND INTERVAL 1 SECOND PRECEDING),
          w180 AS (PARTITION BY apt ORDER BY t RANGE BETWEEN INTERVAL 180 MINUTE PRECEDING
                                                       AND INTERVAL 1 SECOND PRECEDING)
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
               epoch(m.aobt - m.lobt)      AS aobt_vs_lobt,
               epoch(m.aobt - m.iobt)      AS aobt_vs_iobt,
               plan.plan_eobt_mean60, plan.plan_eobt_mean180,
               plan.plan_lobt_mean60, plan.plan_abs_lobt_mean60, plan.plan_n60,
               coalesce(q.dep_queue, 0) AS dep_queue,
               coalesce(aq.arr_queue, 0) AS arr_queue,
               {', '.join(f'thr.takeoff_prev{w}, thr.landing_prev{w}' for w in WINDOWS_MIN)},
               sd.sched_dep_60, nowc.arr_taxi_mean60, nowc.dep_recov_mean60,
               turn.turnaround_sec, turn.inbound_delay, turn.inbound_taxi_in,
               extract(hour FROM m.mvt_time) AS hr,
               extract(dow  FROM m.mvt_time) AS dow,
               extract(month FROM m.mvt_time) AS mon,
               extract(year FROM m.mvt_time) AS yr {ycol}
        FROM mv m
        LEFT JOIN qtab q ON q.mvt_id = m.mvt_id
        LEFT JOIN atab aq ON aq.mvt_id = m.mvt_id
        LEFT JOIN thr   ON thr.mvt_id = m.mvt_id
        LEFT JOIN sd    ON sd.mvt_id = m.mvt_id
        LEFT JOIN nowc  ON nowc.mvt_id = m.mvt_id
        LEFT JOIN turn  ON turn.mvt_id = m.mvt_id
        LEFT JOIN plan  ON plan.mvt_id = m.mvt_id
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
