"""Ensemble the `normal` term across model families.

Everything else in the pipeline (the p classifier, the LIRF force-D rule,
the unmatched-flight model) is held fixed; only the matched-flight
`normal` prediction is blended. That is where the bulk error lives.

Base models on the matched rows:
  A  LightGBM, residual target  y - recov      (the live model)
  B  CatBoost, residual target  y - recov      (different categorical
                                               treatment: ordered target
                                               statistics, which matters
                                               when stand / runway /
                                               operator carry the signal)
  C  LightGBM, raw target       y              (different formulation)

Discipline: blend weights are fitted on a held-out slice of the FIT months
(Nov + Dec 2025), never on validation, so the reported validation gain is
not selection bias. Base models train on the other eight fit months for
this experiment; the submission path retrains them on all twelve.

    uv run scripts/experiment_ensemble.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.optimize import nnls

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_gbm as M  # noqa: E402

BLEND_MONTHS = (11, 12)          # held-out fit slice for blend weights
CAT_PARAMS = dict(loss_function="RMSE", iterations=600, learning_rate=0.08,
                  depth=8, l2_leaf_reg=3.0, random_seed=7, verbose=False,
                  thread_count=-1, one_hot_max_size=4)


def cat_frame(df: pd.DataFrame) -> pd.DataFrame:
    """CatBoost wants string categoricals, not pandas categories."""
    x = df[M.FEATS].copy()
    for c in M.CATS:
        # pandas 3 keeps missing categoricals as NaN through astype(str);
        # CatBoost rejects NaN in a cat feature, so give it an explicit token.
        x[c] = x[c].astype(str).where(x[c].notna(), "__NA__")
    return x


def fit_bases(a: pd.DataFrame) -> dict:
    from catboost import CatBoostRegressor
    out = {}
    out["A_lgb_resid"] = lgb.train(
        dict(M.PARAMS, seed=7, bagging_seed=7),
        lgb.Dataset(a[M.FEATS], a["y"] - a["recov"], categorical_feature=M.CATS),
        num_boost_round=M.NUM_ROUNDS)
    cb = CatBoostRegressor(**CAT_PARAMS)
    cb.fit(cat_frame(a), a["y"] - a["recov"], cat_features=M.CATS)
    out["B_cat_resid"] = cb
    out["C_lgb_raw"] = lgb.train(
        dict(M.PARAMS, seed=11, bagging_seed=11),
        lgb.Dataset(a[M.FEATS], a["y"], categorical_feature=M.CATS),
        num_boost_round=M.NUM_ROUNDS)
    return out


def predict_bases(bases: dict, x: pd.DataFrame) -> np.ndarray:
    recov = x["recov"].to_numpy()
    cols = [
        bases["A_lgb_resid"].predict(x[M.FEATS]) + recov,
        bases["B_cat_resid"].predict(cat_frame(x)) + recov,
        bases["C_lgb_raw"].predict(x[M.FEATS]),
    ]
    return np.column_stack(cols)


def blend_weights(P: np.ndarray, y: np.ndarray) -> np.ndarray:
    w, _ = nnls(P, y)
    return w / w.sum() if w.sum() > 0 else np.full(P.shape[1], 1 / P.shape[1])


def main() -> None:
    M.enable_groups("adsb,adsb_pos")          # the v18 feature set
    con = duckdb.connect(str(M.DB), read_only=True)
    df = con.sql("""SELECT t.*, w.* EXCLUDE (mvt_id), b.* EXCLUDE (mvt_id) FROM train_feat t
                    LEFT JOIN wx w USING (mvt_id)
                    LEFT JOIN adsb b USING (mvt_id)
                    WHERE t.y IS NOT NULL AND t.y > 0""").df()
    mix = con.sql("SELECT apt, mon, count(*) n FROM rank_feat GROUP BY 1,2").df()
    con.close()
    tot = mix["n"].sum()

    vm = df["mon"].isin([1, 7]) & (df["yr"] == 2025)
    fit, val = df[~vm].copy(), df[vm].copy()
    bm = fit["mon"].isin(BLEND_MONTHS)
    fit_a, fit_b = fit[~bm].copy(), fit[bm].copy()
    print(f"fit-A {len(fit_a):,}  blend-set {len(fit_b):,}  val {len(val):,}")

    # Shared, unchanged parts of the pipeline
    p_clf = M.fit_p(fit)
    p = M.apply_p(val, p_clf)
    D = np.nan_to_num(val["D"].to_numpy(), nan=0.0)
    art_a = fit_a["D"].notna() & (fit_a["y"] - fit_a["D"]).abs().le(M.TOL)
    tr_a = M.prep(fit_a[~art_a].copy())
    art_b = fit_b["D"].notna() & (fit_b["y"] - fit_b["D"]).abs().le(M.TOL)
    tr_b = M.prep(fit_b[~art_b].copy())
    vp = M.prep(val.copy())

    ma = tr_a["recov"].notna()
    a, un = tr_a[ma], tr_a[~ma]
    bu = lgb.train(M.PARAMS, lgb.Dataset(un[M.FEATS_UNMATCHED], un["y"],
                                         categorical_feature=M.CATS),
                   num_boost_round=M.NUM_ROUNDS_UNMATCHED)

    print("training base models on fit-A ...", flush=True)
    bases = fit_bases(a)

    # Blend weights on fit-B (matched, non-artifact rows only)
    mb = tr_b["recov"].notna()
    Pb = predict_bases(bases, tr_b[mb])
    w = blend_weights(Pb, tr_b.loc[mb, "y"].to_numpy())
    names = ["A_lgb_resid", "B_cat_resid", "C_lgb_raw"]
    print("blend weights (fit on Nov+Dec 2025): " +
          ", ".join(f"{n}={x:.3f}" for n, x in zip(names, w)))

    # Validation
    vmask = vp["recov"].notna().to_numpy()
    Pv = predict_bases(bases, vp[vmask])
    pu = bu.predict(vp.loc[~vmask, M.FEATS_UNMATCHED])

    def score(normal_matched: np.ndarray, label: str) -> None:
        normal = np.empty(len(vp)); normal[vmask] = normal_matched; normal[~vmask] = pu
        pred = M.apply_day_fault(M.combine(p, D, normal), val, D, normal, None)
        val["pred"] = pred
        mse = sw = smse = 0.0
        for apt, grp in mix.groupby("apt"):
            keep = grp[grp["n"] >= 0.01 * grp["n"].sum()]
            months = sorted(int(x) for x in keep["mon"].unique())
            wgt = grp["n"].sum() / tot
            s = val[(val["apt"] == apt) & (val["mon"].isin(months))]
            r2 = np.mean((s["pred"] - s["y"]) ** 2)
            mse += wgt * r2
            if apt not in ("LIRF", "LFPG"):
                sw += wgt; smse += wgt * r2
        print(f"  {label:34s} all {mse ** 0.5:7.1f}   stable {(smse / sw) ** 0.5:6.1f}")

    print("\nvalidation (Jan+Jul 2025):")
    for i, n in enumerate(names):
        score(Pv[:, i], n)
    score(Pv @ w, "BLEND (weights from fit-B)")
    score(Pv.mean(axis=1), "equal-weight blend")
    # pairwise correlation of residuals -> how much diversity is there
    yv = vp.loc[vmask, "y"].to_numpy()
    R = Pv - yv[:, None]
    print("\nresidual correlation between base models (lower = more diversity):")
    c = np.corrcoef(R.T)
    for i in range(3):
        for j in range(i + 1, 3):
            print(f"  {names[i]} vs {names[j]}: {c[i, j]:.3f}")


if __name__ == "__main__":
    main()
