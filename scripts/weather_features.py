"""Join METAR observations onto departures and derive weather features.

Writes a `wx` table into data/features.duckdb keyed by mvt_id, so the
weather work can be iterated without rebuilding the whole feature set.

Each departure is matched (ASOF) to the most recent observation at or
before its take-off time, at its own airport, and only if that observation
is within MAX_AGE. Weather during the taxi is what matters; take-off time
is used as the anchor because it is present on every row, including the
unmatched movements that have no AOBT_3_flt.

Derived flags target the mechanisms that actually lengthen taxi-out:

  freezing / de-icing   temperature at or below +3 C, and the combination
                        of that with precipitation, plus explicit SN/FZ
                        codes in the present-weather group
  low visibility        visibility below 1.5 statute miles, the regime
                        where low-visibility procedures start to bite
  strong wind           gusty or strong winds force runway
                        reconfiguration and longer routings

    uv run scripts/weather_features.py
"""

from __future__ import annotations

import duckdb

from s3util import DATA_DIR

WX_DIR = DATA_DIR / "weather"
DB = DATA_DIR / "features.duckdb"
MAX_AGE_MIN = 90          # discard a match older than this
LOW_VIS_MILES = 1.5
COLD_C = 3.0


def main() -> None:
    con = duckdb.connect(str(DB))
    con.sql(f"""
        CREATE OR REPLACE TEMP TABLE metar AS
        SELECT station AS apt,
               strptime(valid, '%Y-%m-%d %H:%M') AT TIME ZONE 'UTC' AS t,
               TRY_CAST(tmpc AS DOUBLE) AS temp_c,
               TRY_CAST(dwpc AS DOUBLE) AS dewp_c,
               TRY_CAST(vsby AS DOUBLE) AS vis_mi,
               TRY_CAST(sknt AS DOUBLE) AS wind_kt,
               -- 'T' means a trace of precipitation, i.e. > 0 but unmeasurable
               CASE WHEN p01i = 'T' THEN 0.001 ELSE TRY_CAST(p01i AS DOUBLE) END AS precip_in,
               coalesce(wxcodes, 'M') AS wx
        FROM read_csv('{WX_DIR}/*.csv', header=true, all_varchar=true,
                      filename=false, ignore_errors=true)
        WHERE valid IS NOT NULL
    """)
    n = con.sql("SELECT count(*) FROM metar").fetchone()[0]
    print(f"metar observations: {n:,}")

    con.sql(f"""
        CREATE OR REPLACE TABLE wx AS
        WITH dep AS (
            SELECT mvt_id, apt, mvt_time FROM train_feat
            UNION ALL
            SELECT mvt_id, apt, mvt_time FROM rank_feat
        ),
        j AS (
            SELECT d.mvt_id, d.mvt_time, m.*
            FROM dep d
            ASOF LEFT JOIN metar m
              ON d.apt = m.apt AND d.mvt_time >= m.t
        )
        SELECT mvt_id,
               CASE WHEN age_min <= {MAX_AGE_MIN} THEN temp_c END   AS wx_temp_c,
               CASE WHEN age_min <= {MAX_AGE_MIN} THEN vis_mi END   AS wx_vis_mi,
               CASE WHEN age_min <= {MAX_AGE_MIN} THEN wind_kt END  AS wx_wind_kt,
               CASE WHEN age_min <= {MAX_AGE_MIN} THEN precip_in END AS wx_precip_in,
               CASE WHEN age_min <= {MAX_AGE_MIN} THEN spread END   AS wx_spread_c,
               CASE WHEN age_min <= {MAX_AGE_MIN} THEN cold END     AS wx_cold,
               CASE WHEN age_min <= {MAX_AGE_MIN} THEN deice END    AS wx_deice_risk,
               CASE WHEN age_min <= {MAX_AGE_MIN} THEN lowvis END   AS wx_lowvis,
               CASE WHEN age_min <= {MAX_AGE_MIN} THEN snow END     AS wx_snow,
               CASE WHEN age_min <= {MAX_AGE_MIN} THEN freezing END AS wx_freezing
        FROM (
            SELECT mvt_id, temp_c, vis_mi, wind_kt, precip_in,
                   date_diff('minute', t, mvt_time) AS age_min,
                   temp_c - dewp_c AS spread,
                   (temp_c <= {COLD_C})::INT AS cold,
                   ((temp_c <= {COLD_C}) AND
                    (coalesce(precip_in, 0) > 0
                     OR wx LIKE '%SN%' OR wx LIKE '%FZ%'
                     OR wx LIKE '%PL%' OR wx LIKE '%GS%'))::INT AS deice,
                   (vis_mi < {LOW_VIS_MILES})::INT AS lowvis,
                   (wx LIKE '%SN%')::INT AS snow,
                   (wx LIKE '%FZ%')::INT AS freezing
            FROM j
        )
    """)
    got = con.sql("SELECT count(*), count(wx_temp_c) FROM wx").fetchone()
    print(f"wx rows: {got[0]:,}   with a usable observation: {got[1]:,} "
          f"({100 * got[1] / got[0]:.1f}%)")
    print(con.sql("""
        SELECT round(avg(wx_temp_c),1) AS mean_temp_c, round(avg(wx_vis_mi),2) AS mean_vis_mi,
               round(100*avg(wx_cold),2) pct_cold,
               round(100*avg(wx_deice_risk),2) pct_deice,
               round(100*avg(wx_lowvis),2) pct_lowvis,
               round(100*avg(wx_snow),2) pct_snow
        FROM wx""").df().to_string())
    con.close()
    print(f"wrote table `wx` into {DB}")


if __name__ == "__main__":
    main()
