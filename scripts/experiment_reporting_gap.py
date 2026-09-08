"""The diagnostics that found v14, reproducible from the training data alone.

For matched flights the target decomposes exactly as

    y = recov + (AOBT_3_flt - BLOCK_TIME_UTC_mvt),   recov known

so the bulk of matched-flight error is the variance of the gap between
NM's off-block stamp and the airport's. This script asks, in order:

  1. Is that gap structured in time?  (correlation with the previous
     hour's gaps at the same airport - a ceiling, since BLOCK is blanked
     for departures in the ranking set)
  2. Does the ARRIVAL reporting gap carry it?  (observable in 2026)
  3. Do LOBT_flt / IOBT_flt carry it - per flight, and as a nowcast over
     the previous hour's OTHER departures?  (observable in 2026)

Answers on the 2025 training data: yes (r 0.12-0.39); no (r ~ 0); yes
(per-flight r 0.45-0.49; the nowcast recovers 70-95% of the ceiling at
eight of ten airports). The features built from (3) took v13 -> v14,
301.70 -> 296.47. See notes/FINDINGS.md for the full discussion, including
the follow-up showing that runway-configuration and taxi-dispersion state
do not explain the residual structure that remains.

    uv run scripts/experiment_reporting_gap.py
"""

from __future__ import annotations

import duckdb
import pandas as pd

from s3util import DATA_DIR

pd.set_option("display.width", 200)

T = f"read_parquet('{DATA_DIR}/training_*.parquet')"

# Matched, non-artifact departures with a plausible gap. `gap` is used as
# the alias throughout because `off`, `offset`, `at` and `share` are all
# DuckDB reserved words (each cost a failed run this week).
DEP = f"""
    SELECT ADEP_mvt AS apt, AOBT_3_flt AS t,
           epoch(AOBT_3_flt - BLOCK_TIME_UTC_mvt) AS gap,
           epoch(AOBT_3_flt - LOBT_flt)   AS d_lobt,
           epoch(AOBT_3_flt - IOBT_flt)   AS d_iobt,
           epoch(AOBT_3_flt - EOBT_1_flt) AS d_eobt
    FROM {T}
    WHERE PHASE_mvt = 'DEP' AND AOBT_3_flt IS NOT NULL AND BLOCK_TIME_UTC_mvt IS NOT NULL
      AND LOBT_flt IS NOT NULL AND IOBT_flt IS NOT NULL AND EOBT_1_flt IS NOT NULL
      AND TAXITIME_SEC_mvt IS NOT NULL
      AND abs(epoch(AOBT_3_flt - BLOCK_TIME_UTC_mvt)) < 3600
      AND (SCHED_TIME_UTC_mvt IS NULL
           OR abs(TAXITIME_SEC_mvt - epoch(MVT_TIME_UTC_mvt - SCHED_TIME_UTC_mvt)) > 60)
"""
ARR = f"""
    SELECT ADES_mvt AS apt, MVT_TIME_UTC_mvt AS t,
           epoch(MVT_TIME_UTC_mvt - ARVT_3_flt) AS gap
    FROM {T}
    WHERE PHASE_mvt = 'ARR' AND ARVT_3_flt IS NOT NULL AND MVT_TIME_UTC_mvt IS NOT NULL
      AND abs(epoch(MVT_TIME_UTC_mvt - ARVT_3_flt)) < 3600
"""
WIN = ("PARTITION BY apt ORDER BY t RANGE BETWEEN INTERVAL {m} MINUTE PRECEDING "
       "AND INTERVAL 1 SECOND PRECEDING")   # excludes the row itself


def main() -> None:
    con = duckdb.connect()

    print("=== 1 & 2. Is the departure reporting gap clustered by hour, and does the "
          "arrival gap carry it? ===")
    print(con.sql(f"""
        WITH s AS (
            SELECT apt, t, 'D' AS kind, gap FROM ({DEP})
            UNION ALL SELECT apt, t, 'A', gap FROM ({ARR})),
        w AS (
            SELECT *,
                avg(gap) FILTER (WHERE kind = 'D') OVER ({WIN.format(m=60)})  AS dep_prev60,
                avg(gap) FILTER (WHERE kind = 'A') OVER ({WIN.format(m=60)})  AS arr_prev60,
                avg(gap) FILTER (WHERE kind = 'A') OVER ({WIN.format(m=180)}) AS arr_prev180
            FROM s)
        SELECT apt, count(*) AS n, round(stddev(gap), 0) AS sd_gap,
               round(corr(gap, dep_prev60), 3)  AS r_prev_hour_gaps_CEILING,
               round(corr(gap, arr_prev60), 3)  AS r_arrival_gap_60,
               round(corr(gap, arr_prev180), 3) AS r_arrival_gap_180
        FROM w WHERE kind = 'D' GROUP BY 1 ORDER BY 1
    """).df().to_string(index=False))

    print("\n=== 3a. Per flight: does AOBT_3 - LOBT split the gap distribution? ===")
    print(con.sql(f"""
        SELECT CASE WHEN d_lobt = 0 THEN 'a AOBT==LOBT exactly'
                    WHEN abs(d_lobt) <= 60 THEN 'b within 1 min'
                    WHEN abs(d_lobt) <= 600 THEN 'c 1-10 min'
                    ELSE 'd >10 min' END AS aobt_vs_lobt,
               count(*) AS n, round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct,
               round(avg(gap), 0) AS mean_gap, round(stddev(gap), 0) AS sd_gap
        FROM ({DEP}) GROUP BY 1 ORDER BY 1
    """).df().to_string(index=False))
    print(con.sql(f"""
        SELECT count(*) AS n, round(stddev(gap), 0) AS sd_gap,
               round(corr(gap, d_lobt), 3) AS r_lobt,
               round(corr(gap, d_iobt), 3) AS r_iobt,
               round(corr(gap, d_eobt), 3) AS r_eobt
        FROM ({DEP})
    """).df().to_string(index=False))

    print("\n=== 3b. Nowcast from the previous hour's OTHER departures (2026-observable) ===")
    print(con.sql(f"""
        WITH w AS (
            SELECT *,
                avg(gap)    OVER ({WIN.format(m=60)}) AS m_gap60,
                avg(d_eobt) OVER ({WIN.format(m=60)}) AS m_eobt60,
                avg(d_lobt) OVER ({WIN.format(m=60)}) AS m_lobt60
            FROM ({DEP}))
        SELECT apt, count(*) AS n,
               round(corr(gap, m_gap60), 3)  AS r_ceiling_prev_gaps,
               round(corr(gap, m_eobt60), 3) AS r_nowcast_eobt,
               round(corr(gap, m_lobt60), 3) AS r_nowcast_lobt
        FROM w GROUP BY 1 ORDER BY 1
    """).df().to_string(index=False))


if __name__ == "__main__":
    main()
