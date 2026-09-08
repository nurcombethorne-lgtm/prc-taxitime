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
# Some genuine targets sit just above 24h: a known data fault puts
# BLOCK_TIME a day early, giving taxitime ~= 86400 + a normal taxi. The
# old 40000 ceiling truncated predictions the mixture legitimately wanted
# to make on high-D rows (validation 346.9s -> 342.8s when lifted; the
# curve saturates by 60000).
CLIP_LO, CLIP_HI = 60.0, 90000.0
P_PARAMS = dict(objective="binary", metric="binary_logloss", learning_rate=0.05,
                num_leaves=63, min_data_in_leaf=200, feature_fraction=0.9,
                bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0,
                verbose=-1, num_threads=0)
P_ROUNDS = 300

CATS = ["apt", "stand", "rwy", "actype", "wake", "operator", "segment", "ades"]
# `unmatched` and `D` belong here even though the mixture also uses them:
# the mixture needs E[y | NOT artifact], and that expectation genuinely
# depends on how delayed the flight is and on whether it has an NM record
# (unmatched non-artifact flights taxi far longer than the fleet average).
NUMS = ["dep_queue", "arr_queue", "takeoff_prev15", "takeoff_prev30", "takeoff_prev60",
        "landing_prev15", "landing_prev30", "landing_prev60",
        "sched_dep_60", "recov", "aobt_vs_eobt", "aobt_vs_lobt", "aobt_vs_iobt",
        "hr", "dow", "mon",
        "unmatched", "D",
        # METAR-derived conditions (IEM archive). De-icing weather roughly
        # doubles mean taxi-out and is invisible in the movement/flight
        # tables. Available for every row, so both models get them.
        "wx_temp_c", "wx_vis_mi", "wx_wind_kt", "wx_precip_in", "wx_spread_c",
        "wx_cold", "wx_deice_risk", "wx_lowvis", "wx_snow", "wx_freezing",
        # Cumulative conditions: a de-icing pad backs up over hours, so the
        # length of the cold spell and what has fallen during it matter more
        # than the reading at this instant, and they cover ~6x more flights.
        "wx_precip_6h", "wx_precip_12h", "wx_precip_cold_12h",
        "wx_deice_frac_6h", "wx_temp_min_12h", "wx_vis_min_3h",
        "wx_vis_mean_3h", "wx_hrs_since_thaw",
        # Airport-state nowcast from the previous hour's OTHER departures:
        # mean AOBT_3 - EOBT_1 (and LOBT variants). Carries most of the
        # hour-level clustering of the NM-vs-airport reporting gap, which is
        # the bulk of matched-flight error and otherwise unobservable in 2026.
        "plan_eobt_mean60", "plan_eobt_mean180", "plan_lobt_mean60",
        "plan_abs_lobt_mean60", "plan_n60",
        # Arrival-derived features are meaningful only after the ADES_mvt fix
        # in features.py; before it they bucketed arrivals by origin airport.
        "arr_taxi_mean60", "dep_recov_mean60",
        # Turnaround: the inbound leg that delivered this aircraft, linked by
        # stand + on-block adjacency (~98% coverage).
        "turnaround_sec", "inbound_delay", "inbound_taxi_in"]
FEATS = CATS + NUMS
# Features that depend on a real pushback time. Unmatched flights have no
# AOBT_3_flt, so the turnaround reference falls back to scheduled time and
# these become unreliable exactly where LIRF's error lives — feeding them to
# the unmatched model cost LIRF ~48s. The matched model keeps them.
MATCHED_ONLY = ["arr_taxi_mean60", "dep_recov_mean60",
                "turnaround_sec", "inbound_delay", "inbound_taxi_in",
                # per-flight plan-revision gaps need the row's own AOBT_3
                "aobt_vs_lobt", "aobt_vs_iobt"]
FEATS_UNMATCHED = [f for f in FEATS if f not in MATCHED_ONLY]

PARAMS = dict(objective="regression", metric="rmse", learning_rate=0.05,
              num_leaves=255, min_data_in_leaf=100, feature_fraction=0.9,
              bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0,
              max_cat_threshold=64, cat_smooth=20, verbose=-1, num_threads=0)
# Tuned on the validation curve: the matched (residual-target) model peaks
# near 400 rounds and overfits beyond, while the unmatched model — a much
# smaller, noisier population — keeps improving to ~800. Averaging a few
# seeds of the matched model shaves a little more variance.
NUM_ROUNDS = 400
NUM_ROUNDS_UNMATCHED = 800
SEEDS = (1, 2, 3)


def prep(df: pd.DataFrame) -> pd.DataFrame:
    for c in CATS:
        df[c] = df[c].astype("category")
    df["unmatched"] = df["unmatched"].astype(int)
    return df


def fit_p(fit: pd.DataFrame):
    """P(target is exactly D), as a calibrated classifier.

    This replaces a lookup binned by (airport, unmatched, D-bin), whose
    thinnest cells held only 7-23 samples. A boosted classifier over the
    full feature set is far better resolved and, measured on validation,
    almost exactly calibrated (predicted 0.010/0.118/0.295/0.598/0.920 vs
    actual 0.008/0.119/0.300/0.596/0.922). Calibration is what matters
    here: the mixture consumes p as a probability, not as a ranking.

    Trained on the feature set that excludes the matched-only columns, so
    the unreliable turnaround reference on unmatched rows cannot mislead
    it (that variant also scored best on validation).
    """
    f = fit[fit["D"].notna()].copy()
    f["is_d"] = ((f["y"] - f["D"]).abs() <= TOL).astype(int)
    f = prep(f)
    return lgb.train(P_PARAMS,
                     lgb.Dataset(f[FEATS_UNMATCHED], f["is_d"],
                                 categorical_feature=CATS),
                     num_boost_round=P_ROUNDS)


def apply_p(df: pd.DataFrame, clf, _unused=None) -> np.ndarray:
    p = clf.predict(prep(df.copy())[FEATS_UNMATCHED])
    p = np.asarray(p, dtype=float).copy()
    p[df["D"].isna().to_numpy()] = 0.0
    return p


def combine(p: np.ndarray, D: np.ndarray, normal: np.ndarray) -> np.ndarray:
    Ds = np.nan_to_num(D, nan=0.0)
    return np.clip(p * Ds + (1 - p) * normal, CLIP_LO, CLIP_HI)


# --- LIRF day-fault rule -------------------------------------------------
# A second corrupted regime, distinct from the scheduled-time fallback: the
# block stamp is the genuine pushback but dated a day early, so
#     y == 86400 + (a normal taxi)
# It is confined to LIRF movements with no NM record whose take-off is many
# hours past schedule. In that subgroup the outcome is almost never a normal
# taxi: it is either exactly D (the fallback) or 86400 + taxi (the day
# fault). The mixture's (1 - p) * normal branch was therefore wrong there by
# tens of thousands of seconds. The oracle analysis put ~56s of validation
# RMSE in a handful of these rows.
DAY_FAULT_APT = "LIRF"
DAY_FAULT_EDGES = [6 * 3600, 12 * 3600, 18 * 3600, 30 * 3600, float("inf")]
DAY_FAULT_SMOOTH = 5     # shrink thin bands toward the pooled shares
# Disabled: validated at -20.5s but scored +2.9s WORSE on the leaderboard
# (v12 319.70 vs v11 316.80). The 2026 extract evidently does not carry the
# day fault at the 2025 rate on these rows, so the 86400 branch overshoots.
# Kept for the record; see notes/FINDINGS.md.
DAY_FAULT_ENABLED = False
# What DID reconcile with v12's leaderboard result: in this subgroup the 2026
# truth is the scheduled-time fallback (y == D), which the unblanked 2026
# arrivals show surviving at the 2025 rate. The mixture's p (0.2-0.9 here)
# hedges toward a "normal" branch that almost never occurs in this subgroup
# (1 of 63 training rows). So predict D outright.
FORCE_D_ENABLED = True


def _day_fault_mask(df: pd.DataFrame) -> np.ndarray:
    return ((df["apt"].astype(str) == DAY_FAULT_APT)
            & df["unmatched"].astype(bool)
            & (df["D"] >= DAY_FAULT_EDGES[0])).to_numpy()


def fit_day_fault(fit: pd.DataFrame) -> list[tuple[float, float]]:
    """Per-D-band shares (q = P[y==D], r = P[y==86400+taxi]) on fit rows."""
    m = _day_fault_mask(fit)
    f = fit[m]
    is_d = ((f["y"] - f["D"]).abs() <= TOL)
    is_24 = ((f["y"] - 86400).abs() < 3600) & ~is_d
    q0, r0 = float(is_d.mean()), float(is_24.mean())
    out = []
    for lo, hi in zip(DAY_FAULT_EDGES[:-1], DAY_FAULT_EDGES[1:]):
        b = (f["D"] >= lo) & (f["D"] < hi)
        n = int(b.sum())
        q = (is_d[b].sum() + DAY_FAULT_SMOOTH * q0) / (n + DAY_FAULT_SMOOTH)
        r = (is_24[b].sum() + DAY_FAULT_SMOOTH * r0) / (n + DAY_FAULT_SMOOTH)
        out.append((float(q), float(r)))
    return out


def apply_day_fault(pred: np.ndarray, df: pd.DataFrame, D: np.ndarray,
                    normal: np.ndarray, shares) -> np.ndarray:
    m = _day_fault_mask(df)
    if FORCE_D_ENABLED and m.any():
        out = pred.copy()
        out[m] = np.clip(D[m], CLIP_LO, CLIP_HI)
        return out
    if not DAY_FAULT_ENABLED or not m.any():
        return pred
    out = pred.copy()
    band = np.digitize(D, DAY_FAULT_EDGES[1:-1])   # 0..len(shares)-1
    q = np.array([shares[i][0] for i in band])
    r = np.array([shares[i][1] for i in band])
    three_way = q * D + r * (86400 + normal) + (1 - q - r) * normal
    out[m] = np.clip(three_way[m], CLIP_LO, CLIP_HI)
    return out


def fit_normal(train: pd.DataFrame) -> dict:
    """Two boosters for the non-artifact ("normal") term.

    For flights with an NM record, `recov` (take-off minus actual
    off-block) is already very close to the answer, and a tree cannot
    represent `y = recov + correction` because its leaves emit constants.
    So that model is trained on the RESIDUAL `y - recov` and `recov` is
    added back at predict time, which is a far easier target to fit.

    Unmatched flights have no `recov`, so they get their own model on the
    raw target.
    """
    m = train["recov"].notna()
    a, b = train[m], train[~m]
    out = {"matched": []}
    for seed in SEEDS:
        prm = dict(PARAMS, seed=seed, bagging_seed=seed, feature_fraction_seed=seed)
        out["matched"].append(lgb.train(
            prm, lgb.Dataset(a[FEATS], a["y"] - a["recov"],
                             categorical_feature=CATS),
            num_boost_round=NUM_ROUNDS))
    out["unmatched"] = lgb.train(
        PARAMS, lgb.Dataset(b[FEATS_UNMATCHED], b["y"], categorical_feature=CATS),
        num_boost_round=NUM_ROUNDS_UNMATCHED) if len(b) > 500 else None
    out["fallback"] = float(b["y"].mean()) if len(b) else float(train["y"].mean())
    return out


def predict_normal(boosters: dict, df: pd.DataFrame) -> np.ndarray:
    out = np.empty(len(df), dtype=float)
    m = df["recov"].notna().to_numpy()
    if m.any():
        sub = df.loc[m, FEATS]
        avg = np.mean([bst.predict(sub) for bst in boosters["matched"]], axis=0)
        out[m] = avg + df.loc[m, "recov"].to_numpy()
    if (~m).any():
        if boosters["unmatched"] is not None:
            out[~m] = boosters["unmatched"].predict(df.loc[~m, FEATS_UNMATCHED])
        else:
            out[~m] = boosters["fallback"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = duckdb.connect(str(DB), read_only=True)
    df = con.sql("""SELECT t.*, w.* EXCLUDE (mvt_id)
                    FROM train_feat t LEFT JOIN wx w USING (mvt_id)
                    WHERE t.y IS NOT NULL AND t.y > 0""").df()
    con.close()

    val_mask = df["mon"].isin([1, 7]) & (df["yr"] == 2025)
    fit, val = df[~val_mask].copy(), df[val_mask].copy()
    print(f"fit {len(fit):,} / val {len(val):,}")

    p_clf = fit_p(fit)

    art = fit["D"].notna() & (fit["y"] - fit["D"]).abs().le(TOL)
    train = prep(fit[~art].copy())
    print(f"gbm trains on {len(train):,} non-artifact rows")
    boosters = fit_normal(train)

    valp = prep(val.copy())
    normal = predict_normal(boosters, valp)
    pred = combine(apply_p(val, p_clf), val["D"].to_numpy(), normal)
    shares = fit_day_fault(fit)
    pred = apply_day_fault(pred, val, np.nan_to_num(val["D"].to_numpy(), nan=0.0),
                           normal, shares)
    val["pred"] = pred
    print("day-fault shares per D band (q=P[y==D], r=P[y==86400+taxi]):",
          [(round(q, 2), round(r, 2)) for q, r in shares])

    # Leaderboard composition: which months each airport appears in.
    con = duckdb.connect(str(DB), read_only=True)
    mix = con.sql("""SELECT apt, mon, count(*) n FROM rank_feat GROUP BY 1,2""").df()
    con.close()
    total = mix["n"].sum()

    print(f"\n{'apt':6s} {'months':>8s} {'RMSE':>8s} {'share':>7s}")
    mse = 0.0
    # LIRF and LFPG are dominated by a handful of corrupted records, so the
    # headline number swings on luck. The stable-airport figure is the one to
    # judge a feature change by: it rejected nothing that later transferred.
    stable_w = 0.0
    stable_mse = 0.0
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
        if apt not in ("LIRF", "LFPG"):
            stable_w += w
            stable_mse += w * r * r
        print(f"{apt:6s} {str(months):>8s} {r:8.1f} {w:7.1%}")
    print(f"\nranking-weighted estimate: {mse ** 0.5:.1f}s"
          f"   stable-only (excl LIRF/LFPG): {(stable_mse / stable_w) ** 0.5:.1f}s")
    print("  scored: v1 511.88  v2 458.24  v3 314.42  v4 300.48  v5 297.01  v7 292.22")

    imp = pd.Series(boosters["matched"][0].feature_importance("gain"),
                    index=FEATS).sort_values(ascending=False)
    print("\ntop features by gain:")
    print((imp / imp.sum() * 100).head(10).round(1).to_string())

    if args.dry_run:
        return

    # Refit on all twelve months, predict ranking.
    p_clf = fit_p(df)
    art_all = df["D"].notna() & (df["y"] - df["D"]).abs().le(TOL)
    full = prep(df[~art_all].copy())
    boosters = fit_normal(full)

    con = duckdb.connect(str(DB), read_only=True)
    rk = con.sql("""SELECT r.*, w.* EXCLUDE (mvt_id)
                    FROM rank_feat r LEFT JOIN wx w USING (mvt_id)""").df()
    con.close()
    rkp = prep(rk.copy())
    for c in CATS:  # align category levels with training
        rkp[c] = rkp[c].cat.set_categories(full[c].cat.categories)
    normal_r = predict_normal(boosters, rkp)
    rk["pred"] = combine(apply_p(rk, p_clf), rk["D"].to_numpy(), normal_r)
    rk["pred"] = apply_day_fault(rk["pred"].to_numpy(), rk,
                                 np.nan_to_num(rk["D"].to_numpy(), nan=0.0),
                                 normal_r, fit_day_fault(df))

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
