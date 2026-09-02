"""v4: scheduled-time fallback regime, conditioned on NM-match status.

Discovery: BLOCK_TIME_UTC_mvt is sometimes not a captured pushback event
but a fallback to SCHED_TIME_UTC_mvt. Since
TAXITIME_SEC_mvt == takeoff - BLOCK_TIME_UTC_mvt exactly, those rows have

    taxitime == takeoff - SCHED_TIME_UTC_mvt  ==  D

and BOTH takeoff and SCHED_TIME_UTC_mvt are given in the ranking set, so
D is observable. At LIRF this regime accounts for 89% of the >2h flights,
which alone are 81% of that airport's squared error.

The target therefore mixes two regimes:

    taxitime = D                    with probability p
             = ordinary taxi time   otherwise

so the RMSE-optimal prediction is the mixture mean

    pred = p(apt, D) * D + (1 - p(apt, D)) * normal(apt, stand, rwy)

p is estimated per airport over bins of D as the empirical frequency of
|taxitime - D| <= TOL, and `normal` is the hierarchical group mean fitted
on non-regime rows only, so the fallback rows do not pollute it.

Usage:
    uv run scripts/baseline_v3.py --dry-run   # validate only
    uv run scripts/baseline_v3.py             # + write submission
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from s3util import DATA_DIR, TEAM_NAME

SUBMISSIONS = Path(__file__).resolve().parent.parent / "submissions"
VAL_MONTHS = ("2025-01-01", "2025-07-01")
MIN_GROUP = 10
TOL = 60.0          # |taxitime - D| <= TOL counts as "the answer is D"
CLIP_LO, CLIP_HI = 60.0, 40000.0
# Bin edges for D (seconds). Fine near zero, coarse in the long tail.
D_EDGES = [0, 600, 1200, 1800, 2700, 3600, 5400, 7200, 10800, 14400, 21600, 43200]

SEL = """
    MVT_ID_mvt, ADEP_mvt AS apt, STAND_mvt AS stand, RUNWAY_mvt AS rwy,
    epoch(MVT_TIME_UTC_mvt - SCHED_TIME_UTC_mvt) AS D,
    (AOBT_3_flt IS NULL) AS unmatched
"""
SEL_TRAIN = SEL + ", TAXITIME_SEC_mvt::DOUBLE AS y"


def d_bin_sql(col: str = "D") -> str:
    """CASE expression mapping D to a bin index (0 = negative/missing)."""
    whens = [f"WHEN {col} < {D_EDGES[0]} THEN 0"]
    for i, edge in enumerate(D_EDGES[1:], start=1):
        whens.append(f"WHEN {col} < {edge} THEN {i}")
    return "CASE " + " ".join(whens) + f" ELSE {len(D_EDGES)} END"


def fit_tables(con: duckdb.DuckDBPyConnection) -> float:
    """Build p-by-(airport, D bin) and the non-regime group means."""
    con.sql(f"""CREATE OR REPLACE TEMP TABLE p_tab AS
        SELECT apt, unmatched, {d_bin_sql()} AS db,
               avg((abs(y - D) <= {TOL})::INT)::DOUBLE AS p,
               count(*) AS n
        FROM fit WHERE D IS NOT NULL
        GROUP BY 1, 2, 3 HAVING count(*) >= 20""")
    # Per-airport marginal fallback for thin (airport, unmatched, bin) cells.
    # Deliberately NOT pooled across airports: the scheduled-time fallback is
    # a LIRF-specific data behaviour, so pooling leaks its high rate into
    # airports that never exhibit it.
    con.sql(f"""CREATE OR REPLACE TEMP TABLE p_glob AS
        SELECT apt, unmatched, avg((abs(y - D) <= {TOL})::INT)::DOUBLE AS p
        FROM fit WHERE D IS NOT NULL GROUP BY 1, 2""")

    nonreg = f"(D IS NULL OR abs(y - D) > {TOL})"
    con.sql(f"""CREATE OR REPLACE TEMP TABLE n_asr AS
        SELECT apt, stand, rwy, avg(y) p FROM fit WHERE {nonreg}
        GROUP BY 1,2,3 HAVING count(*) >= {MIN_GROUP}""")
    con.sql(f"""CREATE OR REPLACE TEMP TABLE n_ar AS
        SELECT apt, rwy, avg(y) p FROM fit WHERE {nonreg} GROUP BY 1,2""")
    con.sql(f"""CREATE OR REPLACE TEMP TABLE n_a AS
        SELECT apt, avg(y) p FROM fit WHERE {nonreg} GROUP BY 1""")
    return con.sql(f"SELECT avg(y) FROM fit WHERE {nonreg}").fetchone()[0]


def predict_sql(src: str, g: float) -> str:
    return f"""
        SELECT v.*,
          greatest({CLIP_LO}, least({CLIP_HI},
            CASE WHEN v.D IS NULL THEN coalesce(a.p, b.p, c.p, {g})
                 ELSE coalesce(pt.p, pg.p, 0.0) * v.D
                    + (1 - coalesce(pt.p, pg.p, 0.0)) * coalesce(a.p, b.p, c.p, {g})
            END)) AS pred
        FROM {src} v
        LEFT JOIN n_asr a ON v.apt=a.apt AND v.stand=a.stand AND v.rwy=a.rwy
        LEFT JOIN n_ar  b ON v.apt=b.apt AND v.rwy=b.rwy
        LEFT JOIN n_a   c ON v.apt=c.apt
        LEFT JOIN p_tab pt ON v.apt=pt.apt AND v.unmatched=pt.unmatched AND pt.db = {d_bin_sql("v.D")}
        LEFT JOIN p_glob pg ON v.apt=pg.apt AND v.unmatched=pg.unmatched
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

    con.sql(f"""CREATE TEMP TABLE fit AS SELECT {SEL_TRAIN}
        FROM read_parquet([{q(fit)}])
        WHERE PHASE_mvt='DEP' AND TAXITIME_SEC_mvt IS NOT NULL AND TAXITIME_SEC_mvt > 0""")
    con.sql(f"""CREATE TEMP TABLE val AS SELECT {SEL_TRAIN}
        FROM read_parquet([{q(val)}])
        WHERE PHASE_mvt='DEP' AND TAXITIME_SEC_mvt IS NOT NULL""")

    g = fit_tables(con)
    shares = dict(con.sql(f"""
        SELECT ADEP_mvt, count(*)::DOUBLE / sum(count(*)) OVER ()
        FROM read_parquet('{DATA_DIR / "ranking.parquet"}')
        WHERE PHASE_mvt='DEP' GROUP BY 1""").fetchall())

    per = con.sql(f"""SELECT apt, sqrt(avg((pred-y)^2)) r, count(*) n
                      FROM ({predict_sql('val', g)}) GROUP BY 1 ORDER BY 1""").fetchall()
    print(f"{'apt':6s} {'v4 RMSE':>9s} {'share':>7s}")
    mse = 0.0
    for apt, r, _n in per:
        w = shares.get(apt, 0.0)
        mse += w * r * r
        print(f"{apt:6s} {r:9.1f} {w:7.1%}")
    print(f"\nranking-weighted v4 estimate: {mse ** 0.5:.1f}s   (v1 scored 511.88)")

    if args.dry_run:
        return

    con.sql(f"""CREATE OR REPLACE TEMP TABLE fit AS SELECT {SEL_TRAIN}
        FROM read_parquet('{DATA_DIR}/training_*.parquet')
        WHERE PHASE_mvt='DEP' AND TAXITIME_SEC_mvt IS NOT NULL AND TAXITIME_SEC_mvt > 0""")
    g = fit_tables(con)
    con.sql(f"""CREATE TEMP TABLE rank_src AS SELECT {SEL}
        FROM read_parquet('{DATA_DIR / "ranking.parquet"}') WHERE PHASE_mvt='DEP'""")

    SUBMISSIONS.mkdir(exist_ok=True)
    out = SUBMISSIONS / f"{TEAM_NAME}_v{len(list(SUBMISSIONS.glob(f'{TEAM_NAME}_v*.parquet'))) + 1}.parquet"
    con.sql(f"""
        COPY (SELECT t.MVT_ID_mvt,
                     round(coalesce(p.pred, {g}))::INTEGER AS TAXITIME_SEC_mvt
              FROM read_parquet('{DATA_DIR / "submitting.parquet"}') t
              LEFT JOIN ({predict_sql('rank_src', g)}) p USING (MVT_ID_mvt))
        TO '{out}' (FORMAT PARQUET)""")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
