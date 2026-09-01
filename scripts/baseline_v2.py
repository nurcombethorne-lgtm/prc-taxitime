"""Improved baseline: per-airport choice between two group predictors.

Two ways to predict TAXITIME_SEC_mvt == takeoff - BLOCK_TIME_UTC_mvt:

  direct : hierarchical mean of taxi time over airport x stand x runway
           (stand/runway capture taxi *distance*, which dominates at most
           airports)
  offset : (takeoff - AOBT_3_flt) + hierarchical mean of
           (AOBT_3_flt - BLOCK_TIME_UTC_mvt)
           (wins where the pushback/hold delay dominates, e.g. LIRF)

Neither wins everywhere, so the strategy is chosen per airport on the
Jan + Jul 2025 validation months, then refit on all twelve months.

Means beat medians here because RMSE is minimised by the conditional
mean. Predictions are clipped to a plausible range.

Reported score is weighted by the *ranking* set's airport mix, which
differs from validation's, so it estimates the leaderboard rather than
the validation average.

Usage:
    uv run scripts/baseline_v2.py            # validate + write submission
    uv run scripts/baseline_v2.py --dry-run  # validate only
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from s3util import DATA_DIR, TEAM_NAME

SUBMISSIONS = Path(__file__).resolve().parent.parent / "submissions"
VAL_MONTHS = ("2025-01-01", "2025-07-01")
MIN_GROUP = 10
CLIP_LO, CLIP_HI = 60.0, 7200.0

BASE_COLS = """
    MVT_ID_mvt, ADEP_mvt AS apt, STAND_mvt AS stand, RUNWAY_mvt AS rwy,
    epoch(MVT_TIME_UTC_mvt - AOBT_3_flt) AS recov
"""
TRAIN_COLS = BASE_COLS + """,
    TAXITIME_SEC_mvt::DOUBLE AS y,
    epoch(AOBT_3_flt - BLOCK_TIME_UTC_mvt) AS aobt_offset
"""


def build_groups(con, prefix: str, col: str, where: str) -> float:
    """Hierarchical group means of `col`; returns the global fallback."""
    con.sql(f"""CREATE OR REPLACE TEMP TABLE {prefix}_asr AS
        SELECT apt, stand, rwy, avg({col}) p FROM fit WHERE {where}
        GROUP BY 1,2,3 HAVING count(*) >= {MIN_GROUP}""")
    con.sql(f"""CREATE OR REPLACE TEMP TABLE {prefix}_ar AS
        SELECT apt, rwy, avg({col}) p FROM fit WHERE {where} GROUP BY 1,2""")
    con.sql(f"""CREATE OR REPLACE TEMP TABLE {prefix}_a AS
        SELECT apt, avg({col}) p FROM fit WHERE {where} GROUP BY 1""")
    return con.sql(f"SELECT avg({col}) FROM fit WHERE {where}").fetchone()[0]


def prediction_sql(src: str, g_direct: float, g_off: float, strategy_sql: str) -> str:
    """Both predictors joined, with `strategy_sql` choosing per airport."""
    return f"""
        SELECT v.*,
          greatest({CLIP_LO}, least({CLIP_HI},
            CASE WHEN {strategy_sql} AND v.recov IS NOT NULL
                 THEN v.recov + coalesce(o_asr.p, o_ar.p, o_a.p, {g_off})
                 ELSE coalesce(d_asr.p, d_ar.p, d_a.p, {g_direct})
            END)) AS pred
        FROM {src} v
        LEFT JOIN d_asr ON v.apt=d_asr.apt AND v.stand=d_asr.stand AND v.rwy=d_asr.rwy
        LEFT JOIN d_ar  ON v.apt=d_ar.apt  AND v.rwy=d_ar.rwy
        LEFT JOIN d_a   ON v.apt=d_a.apt
        LEFT JOIN o_asr ON v.apt=o_asr.apt AND v.stand=o_asr.stand AND v.rwy=o_asr.rwy
        LEFT JOIN o_ar  ON v.apt=o_ar.apt  AND v.rwy=o_ar.rwy
        LEFT JOIN o_a   ON v.apt=o_a.apt
    """


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = duckdb.connect()
    files = sorted(DATA_DIR.glob("training_*.parquet"))
    val = [f for f in files if any(m in f.name for m in VAL_MONTHS)]
    fit = [f for f in files if f not in val]
    q = lambda fs: ", ".join(f"'{f}'" for f in fs)  # noqa: E731

    con.sql(f"""CREATE TEMP TABLE fit AS SELECT {TRAIN_COLS}
        FROM read_parquet([{q(fit)}])
        WHERE PHASE_mvt='DEP' AND TAXITIME_SEC_mvt IS NOT NULL AND TAXITIME_SEC_mvt > 0""")
    con.sql(f"""CREATE TEMP TABLE val AS SELECT {TRAIN_COLS}
        FROM read_parquet([{q(val)}])
        WHERE PHASE_mvt='DEP' AND TAXITIME_SEC_mvt IS NOT NULL""")

    g_direct = build_groups(con, "d", "y", "TRUE")
    g_off = build_groups(con, "o", "aobt_offset", "aobt_offset IS NOT NULL")

    # Score both strategies per airport on the validation months.
    rows = con.sql(f"""
        WITH dir AS (SELECT apt, y, pred FROM ({prediction_sql('val', g_direct, g_off, 'FALSE')})),
             off AS (SELECT apt, y, pred FROM ({prediction_sql('val', g_direct, g_off, 'TRUE')}))
        SELECT d.apt,
               sqrt(avg((d.pred-d.y)^2)) AS rmse_direct,
               (SELECT sqrt(avg((o.pred-o.y)^2)) FROM off o WHERE o.apt=d.apt) AS rmse_offset,
               count(*) AS n
        FROM dir d GROUP BY 1 ORDER BY 1
    """).fetchall()

    # Airport mix of the ranking set — the leaderboard weights, not validation's.
    shares = dict(con.sql("""
        SELECT ADEP_mvt, count(*)::DOUBLE / sum(count(*)) OVER ()
        FROM read_parquet('%s') WHERE PHASE_mvt='DEP' GROUP BY 1
    """ % (DATA_DIR / "ranking.parquet")).fetchall())

    print(f"{'apt':6s} {'direct':>9s} {'offset':>9s}  {'chosen':>7s} {'rank share':>11s}")
    use_offset, mse_direct, mse_best = [], 0.0, 0.0
    for apt, rd, ro, _n in rows:
        w = shares.get(apt, 0.0)
        better_offset = ro < rd
        if better_offset:
            use_offset.append(apt)
        mse_direct += w * rd**2
        mse_best += w * min(rd, ro) ** 2
        print(f"{apt:6s} {rd:9.1f} {ro:9.1f}  {'offset' if better_offset else 'direct':>7s} "
              f"{w:10.1%}")

    print(f"\nranking-weighted estimate:")
    print(f"  all-direct : {mse_direct ** 0.5:.1f}s")
    print(f"  per-airport: {mse_best ** 0.5:.1f}s   "
          f"(offset at: {', '.join(use_offset) or 'none'})")

    if args.dry_run:
        return

    # Refit on all twelve months, then predict the ranking departures.
    con.sql(f"""CREATE OR REPLACE TEMP TABLE fit AS SELECT {TRAIN_COLS}
        FROM read_parquet('{DATA_DIR}/training_*.parquet')
        WHERE PHASE_mvt='DEP' AND TAXITIME_SEC_mvt IS NOT NULL AND TAXITIME_SEC_mvt > 0""")
    g_direct = build_groups(con, "d", "y", "TRUE")
    g_off = build_groups(con, "o", "aobt_offset", "aobt_offset IS NOT NULL")

    con.sql(f"""CREATE TEMP TABLE rank_src AS SELECT {BASE_COLS}
        FROM read_parquet('{DATA_DIR / "ranking.parquet"}') WHERE PHASE_mvt='DEP'""")
    strategy = ("v.apt IN (" + ", ".join(f"'{a}'" for a in use_offset) + ")") if use_offset else "FALSE"
    pred = prediction_sql("rank_src", g_direct, g_off, strategy)

    SUBMISSIONS.mkdir(exist_ok=True)
    n_existing = len(list(SUBMISSIONS.glob(f"{TEAM_NAME}_v*.parquet")))
    out = SUBMISSIONS / f"{TEAM_NAME}_v{n_existing + 1}.parquet"
    template = DATA_DIR / "submitting.parquet"
    con.sql(f"""
        COPY (SELECT t.MVT_ID_mvt,
                     round(coalesce(p.pred, {g_direct}))::INTEGER AS TAXITIME_SEC_mvt
              FROM read_parquet('{template}') t
              LEFT JOIN ({pred}) p USING (MVT_ID_mvt))
        TO '{out}' (FORMAT PARQUET)
    """)
    print(f"\nwrote {out}")
    print(f"next: uv run scripts/validate_submission.py {out}")


if __name__ == "__main__":
    main()
