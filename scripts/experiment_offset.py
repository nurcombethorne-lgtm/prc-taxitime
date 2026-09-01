"""Test the offset reformulation against the direct baseline.

Because TAXITIME_SEC_mvt == takeoff - BLOCK_TIME_UTC_mvt exactly, and
takeoff (MVT_TIME_UTC_mvt) is given in the ranking set, the target
decomposes as:

    taxitime = (takeoff - AOBT_3_flt) + (AOBT_3_flt - BLOCK_TIME_UTC_mvt)
               known                unknown offset

So instead of predicting taxi time directly, predict only the offset and
add the known quantity. AOBT_3_flt is present on 98.5% of ranking
departures; rows without it fall back to the direct baseline.

Scored on Jan + Jul 2025, fit on the other ten months.
"""

from __future__ import annotations

import duckdb

from s3util import DATA_DIR

VAL_MONTHS = ("2025-01-01", "2025-07-01")
MIN_GROUP = 10


def main() -> None:
    con = duckdb.connect()
    files = sorted(DATA_DIR.glob("training_*.parquet"))
    val = [f for f in files if any(m in f.name for m in VAL_MONTHS)]
    fit = [f for f in files if f not in val]
    fit_list = ", ".join(f"'{f}'" for f in fit)
    val_list = ", ".join(f"'{f}'" for f in val)

    base_cols = """
        ADEP_mvt AS apt, STAND_mvt AS stand, RUNWAY_mvt AS rwy,
        TAXITIME_SEC_mvt::DOUBLE AS y,
        epoch(MVT_TIME_UTC_mvt - AOBT_3_flt) AS recov,
        epoch(AOBT_3_flt - BLOCK_TIME_UTC_mvt) AS aobt_offset
    """
    con.sql(f"""CREATE TEMP TABLE fit AS SELECT {base_cols}
        FROM read_parquet([{fit_list}])
        WHERE PHASE_mvt='DEP' AND TAXITIME_SEC_mvt IS NOT NULL AND TAXITIME_SEC_mvt > 0""")
    con.sql(f"""CREATE TEMP TABLE val AS SELECT {base_cols}
        FROM read_parquet([{val_list}])
        WHERE PHASE_mvt='DEP' AND TAXITIME_SEC_mvt IS NOT NULL""")

    def build(col: str, where: str = "TRUE") -> float:
        """Build hierarchical group means over `col` and return global mean."""
        con.sql(f"""CREATE OR REPLACE TEMP TABLE g_asr AS
            SELECT apt, stand, rwy, avg({col}) p FROM fit WHERE {where}
            GROUP BY 1,2,3 HAVING count(*) >= {MIN_GROUP}""")
        con.sql(f"""CREATE OR REPLACE TEMP TABLE g_ar AS
            SELECT apt, rwy, avg({col}) p FROM fit WHERE {where} GROUP BY 1,2""")
        con.sql(f"""CREATE OR REPLACE TEMP TABLE g_a AS
            SELECT apt, avg({col}) p FROM fit WHERE {where} GROUP BY 1""")
        return con.sql(f"SELECT avg({col}) FROM fit WHERE {where}").fetchone()[0]

    def rmse(pred_sql: str, label: str) -> None:
        overall = con.sql(f"SELECT sqrt(avg((pred-y)^2)) FROM ({pred_sql})").fetchone()[0]
        per = con.sql(f"""SELECT apt, sqrt(avg((pred-y)^2)) r, count(*) n
                          FROM ({pred_sql}) GROUP BY 1 ORDER BY 1""").fetchall()
        print(f"\n{label}: overall RMSE {overall:.1f}s")
        for a, r, n in per:
            print(f"   {a}: {r:7.1f}s  (n={n:,})")

    # --- A: direct baseline (v2 = hierarchical mean of taxitime) ---
    g = build("y")
    direct = f"""
        SELECT v.apt, v.y, coalesce(a.p, b.p, c.p, {g}) AS pred
        FROM val v
        LEFT JOIN g_asr a ON v.apt=a.apt AND v.stand=a.stand AND v.rwy=a.rwy
        LEFT JOIN g_ar  b ON v.apt=b.apt AND v.rwy=b.rwy
        LEFT JOIN g_a   c ON v.apt=c.apt
    """
    rmse(direct, "A. direct mean(taxitime)")

    # Keep the direct model's tables under distinct names for the hybrid.
    for t in ("g_asr", "g_ar", "g_a"):
        con.sql(f"CREATE OR REPLACE TEMP TABLE d_{t} AS SELECT * FROM {t}")
    g_direct = g

    # --- B: offset reformulation ---
    g_off = build("aobt_offset", where="aobt_offset IS NOT NULL")
    hybrid = f"""
        SELECT v.apt, v.y,
          CASE WHEN v.recov IS NOT NULL
               THEN v.recov + coalesce(a.p, b.p, c.p, {g_off})
               ELSE coalesce(da.p, db.p, dc.p, {g_direct})
          END AS pred
        FROM val v
        LEFT JOIN g_asr a ON v.apt=a.apt AND v.stand=a.stand AND v.rwy=a.rwy
        LEFT JOIN g_ar  b ON v.apt=b.apt AND v.rwy=b.rwy
        LEFT JOIN g_a   c ON v.apt=c.apt
        LEFT JOIN d_g_asr da ON v.apt=da.apt AND v.stand=da.stand AND v.rwy=da.rwy
        LEFT JOIN d_g_ar  db ON v.apt=db.apt AND v.rwy=db.rwy
        LEFT JOIN d_g_a   dc ON v.apt=dc.apt
    """
    rmse(hybrid, "B. recov + mean(offset)")


if __name__ == "__main__":
    main()
