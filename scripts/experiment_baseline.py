"""Compare aggregation strategies for the hierarchical group baseline.

RMSE is minimised by the conditional *mean*, not the median, so the v1
median baseline is provably suboptimal. But the raw mean is corrupted by
absurd outliers (LIRF has taxi times up to 131,167s = 36h), so this
script compares:

  median            - v1 behaviour
  mean              - RMSE-optimal in theory, fragile in practice
  winsorised mean   - values capped at a percentile before averaging,
                      at several cap levels

Fit on the ten non-Jan/Jul 2025 months, scored on Jan + Jul 2025 (the
seasons the ranking set covers). Groups fall back
airport x stand x runway -> airport x runway -> airport -> global.
"""

from __future__ import annotations

import duckdb

from s3util import DATA_DIR

VAL_MONTHS = ("2025-01-01", "2025-07-01")
MIN_GROUP = 10
CAPS = (1800, 2400, 3000, 3600, 5400, 7200, None)


def main() -> None:
    con = duckdb.connect()
    files = sorted(DATA_DIR.glob("training_*.parquet"))
    val = [f for f in files if any(m in f.name for m in VAL_MONTHS)]
    fit = [f for f in files if f not in val]
    fit_list = ", ".join(f"'{f}'" for f in fit)
    val_list = ", ".join(f"'{f}'" for f in val)

    con.sql(f"""
        CREATE TEMP TABLE fit AS
        SELECT ADEP_mvt AS apt, STAND_mvt AS stand, RUNWAY_mvt AS rwy,
               TAXITIME_SEC_mvt::DOUBLE AS y
        FROM read_parquet([{fit_list}])
        WHERE PHASE_mvt = 'DEP' AND TAXITIME_SEC_mvt IS NOT NULL
          AND TAXITIME_SEC_mvt > 0
    """)
    con.sql(f"""
        CREATE TEMP TABLE val AS
        SELECT ADEP_mvt AS apt, STAND_mvt AS stand, RUNWAY_mvt AS rwy,
               TAXITIME_SEC_mvt::DOUBLE AS y
        FROM read_parquet([{val_list}])
        WHERE PHASE_mvt = 'DEP' AND TAXITIME_SEC_mvt IS NOT NULL
    """)

    print(f"{'strategy':22s} {'overall':>9s}   per-airport worst")
    results = {}
    for cap in CAPS:
        for agg in ("median", "mean"):
            if agg == "median" and cap is not None:
                continue  # median is already robust; capping barely moves it
            label = f"{agg}" if cap is None else f"{agg}@cap{cap}"
            ycap = "y" if cap is None else f"least(y, {cap})"
            con.sql(f"""
                CREATE OR REPLACE TEMP TABLE g_asr AS
                SELECT apt, stand, rwy, {agg}({ycap}) AS p
                FROM fit GROUP BY 1,2,3 HAVING count(*) >= {MIN_GROUP}
            """)
            con.sql(f"""
                CREATE OR REPLACE TEMP TABLE g_ar AS
                SELECT apt, rwy, {agg}({ycap}) AS p FROM fit GROUP BY 1,2
            """)
            con.sql(f"""
                CREATE OR REPLACE TEMP TABLE g_a AS
                SELECT apt, {agg}({ycap}) AS p FROM fit GROUP BY 1
            """)
            gl = con.sql(f"SELECT {agg}({ycap}) FROM fit").fetchone()[0]

            pred = f"""
                SELECT v.apt, v.y, coalesce(a.p, b.p, c.p, {gl}) AS pred
                FROM val v
                LEFT JOIN g_asr a ON v.apt=a.apt AND v.stand=a.stand AND v.rwy=a.rwy
                LEFT JOIN g_ar  b ON v.apt=b.apt AND v.rwy=b.rwy
                LEFT JOIN g_a   c ON v.apt=c.apt
            """
            overall = con.sql(
                f"SELECT sqrt(avg((pred-y)^2)) FROM ({pred})").fetchone()[0]
            worst = con.sql(f"""
                SELECT apt, sqrt(avg((pred-y)^2)) r FROM ({pred})
                GROUP BY 1 ORDER BY r DESC LIMIT 1
            """).fetchone()
            results[label] = overall
            print(f"{label:22s} {overall:9.1f}   {worst[0]} {worst[1]:.0f}s")

    best = min(results, key=results.get)
    print(f"\nbest: {best}  ({results[best]:.1f}s)")
    print(f"v1 (median) was {results['median']:.1f}s -> "
          f"improvement {results['median'] - results[best]:.1f}s")


if __name__ == "__main__":
    main()
