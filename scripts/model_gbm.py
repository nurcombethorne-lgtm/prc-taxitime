"""LightGBM model over queue/congestion features, on top of the
scheduled-fallback mixture.

Prediction keeps the v4 decomposition, replacing the group-mean `normal`
term with a gradient-boosted model that can use the continuous
congestion features:

    pred = p(apt, unmatched, D-bin) * D  +  (1 - p) * gbm(features)

The GBM is trained only on non-artifact rows, so the scheduled-fallback
rows (where the target is mechanically `D`) do not distort it.

Validation matches the leaderboard's composition: the ranking set is
complete for the airport-months it covers, and July 2026 contains ONLY
EDDF, EGLL and EHAM. So each airport is scored on the months it actually
appears in (Jan only for seven of them), then weighted by its share of
ranking rows. Scoring every airport on Jan+Jul overweights summer for
airports the leaderboard only sees in winter.

Usage:
    uv run scripts/model_gbm.py --dry-run
    uv run scripts/model_gbm.py            # + write submission
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd

from s3util import DATA_DIR, TEAM_NAME

SUBMISSIONS = Path(__file__).resolve().parent.parent / "submissions"
DB = DATA_DIR / "features.duckdb"
TOL = 60.0
CLIP_LO, CLIP_HI = 60.0, 40000.0
D_EDGES = [0, 600, 1200, 1800, 2700, 3600, 5400, 7200, 10800, 14400, 21600, 43200]

CATS = ["apt", "stand", "rwy", "actype", "wake", "operator", "segment", "ades"]
NUMS = ["dep_queue", "takeoff_prev15", "takeoff_prev30", "takeoff_prev60",
        "landing_prev15", "landing_prev30", "landing_prev60",
        "sched_dep_60", "recov", "aobt_vs_eobt", "hr", "dow", "mon"]
FEATS = CATS + NUMS

PARAMS = dict(objective="regression", metric="rmse", learning_rate=0.05,
              num_leaves=255, min_data_in_leaf=100, feature_fraction=0.9,
              bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0,
              max_cat_threshold=64, cat_smooth=20, verbose=-1, num_threads=0)
NUM_ROUNDS = 900


def d_bin(s: pd.Series) -> pd.Series:
    return pd.Series(np.digitize(s.fillna(-1e9), D_EDGES), index=s.index)


def prep(df: pd.DataFrame) -> pd.DataFrame:
    for c in CATS:
        df[c] = df[c].astype("category")
    return df


def fit_p(fit: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """p = P(target is exactly D), by (apt, unmatched, D-bin), with a
    per-airport fallback. Deliberately not pooled across airports: the
    scheduled-time fallback is a LIRF-specific behaviour."""
    f = fit[fit["D"].notna()].copy()
    f["db"] = d_bin(f["D"])
    f["is_d"] = (f["y"] - f["D"]).abs().le(TOL).astype(float)
    cell = (f.groupby(["apt", "unmatched", "db"], observed=True)
              .agg(p=("is_d", "mean"), n=("is_d", "size")).reset_index())
    cell = cell[cell["n"] >= 20][["apt", "unmatched", "db", "p"]]
    apt_fb = (f.groupby(["apt", "unmatched"], observed=True)
                .agg(p_fb=("is_d", "mean")).reset_index())
    return cell, apt_fb


def apply_p(df: pd.DataFrame, cell: pd.DataFrame, apt_fb: pd.DataFrame) -> np.ndarray:
    d = df[["apt", "unmatched", "D"]].copy()
    d["db"] = d_bin(d["D"])
    d = d.merge(cell, on=["apt", "unmatched", "db"], how="left")
    d = d.merge(apt_fb, on=["apt", "unmatched"], how="left")
    p = d["p"].fillna(d["p_fb"]).fillna(0.0).to_numpy(copy=True)
    p[d["D"].isna().to_numpy()] = 0.0
    return p


def combine(p: np.ndarray, D: np.ndarray, normal: np.ndarray) -> np.ndarray:
    Ds = np.nan_to_num(D, nan=0.0)
    return np.clip(p * Ds + (1 - p) * normal, CLIP_LO, CLIP_HI)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = duckdb.connect(str(DB), read_only=True)
    df = con.sql("SELECT * FROM train_feat WHERE y IS NOT NULL AND y > 0").df()
    con.close()

    val_mask = df["mon"].isin([1, 7]) & (df["yr"] == 2025)
    fit, val = df[~val_mask].copy(), df[val_mask].copy()
    print(f"fit {len(fit):,} / val {len(val):,}")

    cell, apt_fb = fit_p(fit)

    art = fit["D"].notna() & (fit["y"] - fit["D"]).abs().le(TOL)
    train = prep(fit[~art].copy())
    print(f"gbm trains on {len(train):,} non-artifact rows")
    booster = lgb.train(PARAMS, lgb.Dataset(train[FEATS], train["y"],
                                            categorical_feature=CATS),
                        num_boost_round=NUM_ROUNDS)

    valp = prep(val.copy())
    normal = booster.predict(valp[FEATS])
    pred = combine(apply_p(val, cell, apt_fb), val["D"].to_numpy(), normal)
    val["pred"] = pred

    # Leaderboard composition: which months each airport appears in.
    con = duckdb.connect(str(DB), read_only=True)
    mix = con.sql("""SELECT apt, mon, count(*) n FROM rank_feat GROUP BY 1,2""").df()
    con.close()
    total = mix["n"].sum()

    print(f"\n{'apt':6s} {'months':>8s} {'RMSE':>8s} {'share':>7s}")
    mse = 0.0
    for apt, grp in mix.groupby("apt"):
        # The ranking set has a handful of spillover rows in Feb/Aug (a
        # take-off just past midnight on the 1st). Including those months
        # would drag a whole extra validation month in, so keep only
        # months that are a real part of this airport's ranking rows.
        keep = grp[grp["n"] >= 0.01 * grp["n"].sum()]
        months = sorted(int(m) for m in keep["mon"].unique())
        w = grp["n"].sum() / total
        sub = val[(val["apt"] == apt) & (val["mon"].isin(months))]
        r = float(np.sqrt(np.mean((sub["pred"] - sub["y"]) ** 2)))
        mse += w * r * r
        print(f"{apt:6s} {str(months):>8s} {r:8.1f} {w:7.1%}")
    print(f"\nranking-weighted GBM estimate: {mse ** 0.5:.1f}s   "
          f"(v1 511.88, v2 458.24)")

    imp = pd.Series(booster.feature_importance("gain"), index=FEATS).sort_values(ascending=False)
    print("\ntop features by gain:")
    print((imp / imp.sum() * 100).head(10).round(1).to_string())

    if args.dry_run:
        return

    # Refit on all twelve months, predict ranking.
    cell, apt_fb = fit_p(df)
    art_all = df["D"].notna() & (df["y"] - df["D"]).abs().le(TOL)
    full = prep(df[~art_all].copy())
    booster = lgb.train(PARAMS, lgb.Dataset(full[FEATS], full["y"],
                                            categorical_feature=CATS),
                        num_boost_round=NUM_ROUNDS)

    con = duckdb.connect(str(DB), read_only=True)
    rk = con.sql("SELECT * FROM rank_feat").df()
    con.close()
    rkp = prep(rk.copy())
    for c in CATS:  # align category levels with training
        rkp[c] = rkp[c].cat.set_categories(full[c].cat.categories)
    normal_r = booster.predict(rkp[FEATS])
    rk["pred"] = combine(apply_p(rk, cell, apt_fb), rk["D"].to_numpy(), normal_r)

    SUBMISSIONS.mkdir(exist_ok=True)
    out = SUBMISSIONS / f"{TEAM_NAME}_v{len(list(SUBMISSIONS.glob(f'{TEAM_NAME}_v*.parquet'))) + 1}.parquet"
    tmpl = duckdb.connect()
    tmpl.register("preds", rk[["mvt_id", "pred"]])
    tmpl.sql(f"""
        COPY (SELECT t.MVT_ID_mvt,
                     round(coalesce(p.pred, 900))::INTEGER AS TAXITIME_SEC_mvt
              FROM read_parquet('{DATA_DIR / "submitting.parquet"}') t
              LEFT JOIN preds p ON p.mvt_id = t.MVT_ID_mvt)
        TO '{out}' (FORMAT PARQUET)""")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
