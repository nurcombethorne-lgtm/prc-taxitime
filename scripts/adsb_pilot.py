"""Pilot: does an ADS-B ground trace locate the airport's block time better
than AOBT_3_flt does?

Matches extracted segments (adsb_extract.py) to that day's departures by
callsign and lift-off time, then compares each candidate off-block time
with BLOCK_TIME_UTC_mvt (truth) and with AOBT_3_flt.

    uv run scripts/adsb_pilot.py data/adsb/segments_2025-07-15.parquet 2025-07-15
"""
from __future__ import annotations

import sys

import duckdb
import numpy as np
import pandas as pd

pd.set_option("display.width", 220)

seg_path, day = sys.argv[1], sys.argv[2]
mon = day[:7] + "-01"
c = duckdb.connect()
c.sql(f"""CREATE TABLE dep AS
  SELECT MVT_ID_mvt mvt_id, ADEP_mvt apt, upper(trim(CALLSIGN_flt)) cs, FLIGHT_mvt flt,
         epoch(MVT_TIME_UTC_mvt) t_off, epoch(BLOCK_TIME_UTC_mvt) t_block,
         epoch(AOBT_3_flt) t_aobt3, epoch(SCHED_TIME_UTC_mvt) t_sched, TAXITIME_SEC_mvt y,
         STAND_mvt stand
  FROM 'data/training_{mon}_*.parquet'
  WHERE PHASE_mvt='DEP' AND MVT_TIME_UTC_mvt::DATE = DATE '{day}'""")
c.sql(f"""CREATE TABLE seg AS SELECT *, upper(trim(callsign)) cs FROM '{seg_path}'""")
n_dep = c.sql("SELECT count(*) FROM dep").fetchone()[0]
print(f"departures on {day}: {n_dep:,}   segments: {c.sql('SELECT count(*) FROM seg').fetchone()[0]:,}")

# Match: same airport, same callsign, lift-off within 3 min of the movement
# take-off time; if several, the closest lift-off. Fallback: no lift-off seen
# but last ground point within 6 min before take-off.
c.sql("""CREATE TABLE m AS
  SELECT * FROM (
    SELECT d.*, s.hex, s.t_first_ground, s.t_first_moving, s.t_last_ground, s.t_first_air,
           s.n_ground, s.stale_first, s.gs_first,
           coalesce(abs(s.t_first_air - d.t_off), abs(s.t_last_ground - d.t_off) + 60) AS dist,
           row_number() OVER (PARTITION BY d.mvt_id ORDER BY
               coalesce(abs(s.t_first_air - d.t_off), abs(s.t_last_ground - d.t_off) + 60)) rn
    FROM dep d JOIN seg s ON s.apt = d.apt AND s.cs = d.cs
    WHERE (s.t_first_air IS NOT NULL AND abs(s.t_first_air - d.t_off) <= 180)
       OR (s.t_first_air IS NULL AND d.t_off - s.t_last_ground BETWEEN 0 AND 360)
  ) WHERE rn = 1""")
print("\n=== coverage: departures with a matched ground trace ===")
print(c.sql("""SELECT d.apt, count(*) n_dep, count(m.mvt_id) n_matched,
   round(100.0*count(m.mvt_id)/count(*),1) pct_matched,
   round(100.0*count(m.t_first_moving)/count(*),1) pct_with_moving,
   round(100.0*avg(CASE WHEN m.mvt_id IS NOT NULL THEN (m.t_first_ground <= d.t_block + 60)::INT END),1) pct_trace_starts_by_block
   FROM dep d LEFT JOIN m USING (mvt_id) GROUP BY 1 ORDER BY 1""").df().to_string(index=False))

print("\n=== accuracy vs BLOCK_TIME (truth), matched rows only; err = candidate - block, seconds ===")
q = """SELECT apt, count(*) n,
   round(median(t_aobt3 - t_block)) med_aobt3, round(sqrt(avg((t_aobt3 - t_block)^2))) rmse_aobt3,
   round(100.0*avg((abs(t_aobt3 - t_block)<=60)::INT),1) pct60_aobt3,
   round(median(t_first_ground - t_block)) med_firstobs, round(sqrt(avg((t_first_ground - t_block)^2))) rmse_firstobs,
   round(100.0*avg((abs(t_first_ground - t_block)<=60)::INT),1) pct60_firstobs,
   round(median(t_first_moving - t_block)) med_moving, round(sqrt(avg((t_first_moving - t_block)^2))) rmse_moving,
   round(100.0*avg((abs(t_first_moving - t_block)<=60)::INT),1) pct60_moving
   FROM m WHERE t_aobt3 IS NOT NULL AND abs(y - (t_off - t_sched)) > 120 GROUP BY 1 ORDER BY 1"""
print("(excluding scheduled-time fallback rows, whose block is not a real off-block)")
print(c.sql(q).df().to_string(index=False))

print("\n=== on the rows where AOBT_3 is far from block (|gap|>10 min): does the trace side with block? ===")
print(c.sql("""SELECT apt, count(*) n,
   round(median(abs(t_aobt3 - t_block))) med_abs_gap_aobt3,
   round(median(abs(t_first_moving - t_block))) med_abs_gap_moving,
   round(100.0*avg((abs(t_first_moving - t_block) < abs(t_aobt3 - t_block))::INT),1) pct_trace_closer
   FROM m WHERE t_aobt3 IS NOT NULL AND t_first_moving IS NOT NULL AND abs(t_aobt3 - t_block) > 600
     AND abs(y - (t_off - t_sched)) > 120 GROUP BY 1 ORDER BY 1""").df().to_string(index=False))

print("\n=== unmatched-to-NM departures (no AOBT_3): trace coverage and accuracy ===")
print(c.sql("""SELECT d.apt, count(*) n_unm, count(m.mvt_id) n_traced,
   round(median(m.t_first_moving - d.t_block)) med_moving_err,
   round(100.0*avg(CASE WHEN m.t_first_moving IS NOT NULL THEN (abs(m.t_first_moving - d.t_block)<=120)::INT END),1) pct120_moving
   FROM dep d LEFT JOIN m USING (mvt_id) WHERE d.t_aobt3 IS NULL GROUP BY 1 ORDER BY 1""").df().to_string(index=False))

print("\n=== headline: taxi-out RMSE if predicted as t_off - candidate (matched, non-fallback rows) ===")
print(c.sql("""SELECT apt, count(*) n,
   round(sqrt(avg((t_off - t_aobt3 - y)^2))) rmse_from_aobt3,
   round(sqrt(avg((t_off - t_first_moving - y)^2))) rmse_from_moving,
   round(sqrt(avg((t_off - t_first_ground - y)^2))) rmse_from_firstobs,
   round(sqrt(avg((t_off - least(t_aobt3, t_first_moving) - y)^2))) rmse_from_earlier_of_two
   FROM m WHERE t_aobt3 IS NOT NULL AND t_first_moving IS NOT NULL AND abs(y - (t_off - t_sched)) > 120
   GROUP BY 1 ORDER BY 1""").df().to_string(index=False))
