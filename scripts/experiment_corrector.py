"""Out-of-fold corrector (stacking), folds by month.

A second-stage model learns the base pipeline's out-of-time error: for
each fit month the base pipeline is trained on the other fit months and
predicts the held-out one; the corrector is then trained on those
out-of-fold rows to predict `y - base_pred` from the features plus the
base outputs. Validation: base pipeline fitted on all ten fit months,
corrector applied to its January/July predictions.

    uv run scripts/experiment_corrector.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_gbm as M  # noqa: E402

GROUPS = "adsb,adsb_pos"
CORR = dict(objective="regression", metric="rmse", learning_rate=0.03, num_leaves=31,
            min_data_in_leaf=500, feature_fraction=0.8, bagging_fraction=0.8,
            bagging_freq=1, lambda_l2=10.0, verbose=-1, num_threads=0, seed=3)
CORR_ROUNDS = 300
RES_CAP = 20000.0       # rows whose base error exceeds this are lottery rows; not taught


def pipeline(fit: pd.DataFrame, target: pd.DataFrame) -> pd.DataFrame:
    p_clf = M.fit_p(fit)
    art = fit["D"].notna() & (fit["y"] - fit["D"]).abs().le(M.TOL)
    boosters = M.fit_normal(M.prep(fit[~art].copy()))
    tp = M.prep(target.copy())
    normal = M.predict_normal(boosters, tp)
    p = M.apply_p(target, p_clf)
    D = np.nan_to_num(target["D"].to_numpy(), nan=0.0)
    pred = M.apply_day_fault(M.combine(p, target["D"].to_numpy(), normal), target, D, normal, None)
    out = target.copy()
    out["base_pred"], out["base_p"], out["base_normal"] = pred, p, normal
    return out


def main() -> None:
    M.enable_groups(GROUPS)
    M.SEEDS = (1,)                                  # one seed per fold keeps this to ~40 min
    con = duckdb.connect(str(M.DB), read_only=True)
    df = con.sql("""SELECT t.*, w.* EXCLUDE (mvt_id), b.* EXCLUDE (mvt_id) FROM train_feat t
                    LEFT JOIN wx w USING (mvt_id) LEFT JOIN adsb b USING (mvt_id)
                    WHERE t.y IS NOT NULL AND t.y > 0""").df()
    mix = con.sql("SELECT apt, mon, count(*) n FROM rank_feat GROUP BY 1,2").df()
    con.close()
    tot = mix["n"].sum()
    vm = df["mon"].isin([1, 7]) & (df["yr"] == 2025)
    fit, val = df[~vm].copy(), df[vm].copy()

    oof = []
    for m in sorted(fit["mon"].unique()):
        print(f"fold: month {int(m)}", flush=True)
        oof.append(pipeline(fit[fit["mon"] != m], fit[fit["mon"] == m]))
    oof = pd.concat(oof)
    M.SEEDS = (1, 2, 3)
    valb = pipeline(fit, val)

    feats = list(dict.fromkeys(M.FEATS + ["base_pred", "base_p", "base_normal"]))
    tr = M.prep(oof.copy())
    tr["t"] = tr["y"] - tr["base_pred"]
    tr = tr[tr["t"].abs() <= RES_CAP]
    corr = lgb.train(CORR, lgb.Dataset(tr[feats], tr["t"], categorical_feature=M.CATS),
                     num_boost_round=CORR_ROUNDS)
    vp = M.prep(valb.copy())
    delta = corr.predict(vp[feats])
    print(f"oof rows {len(tr):,}; oof base RMSE {np.sqrt(np.mean(tr['t']**2)):.1f}; "
          f"mean |delta| on val {np.mean(np.abs(delta)):.1f}")

    def score(pred: np.ndarray, label: str) -> None:
        v = valb.assign(pred=np.clip(pred, M.CLIP_LO, M.CLIP_HI))
        mse = sw = smse = 0.0
        parts = {}
        for apt, grp in mix.groupby("apt"):
            keep = grp[grp["n"] >= 0.01 * grp["n"].sum()]
            months = sorted(int(x) for x in keep["mon"].unique())
            wgt = grp["n"].sum() / tot
            s = v[(v["apt"] == apt) & (v["mon"].isin(months))]
            r2 = float(np.mean((s["pred"] - s["y"]) ** 2))
            mse += wgt * r2
            parts[apt] = r2 ** 0.5
            if apt not in ("LIRF", "LFPG"):
                sw += wgt; smse += wgt * r2
        st = v[~v["apt"].isin(["LIRF", "LFPG"])]
        jan = np.sqrt(np.mean((st[st.mon == 1].pred - st[st.mon == 1].y) ** 2))
        jul = np.sqrt(np.mean((st[st.mon == 7].pred - st[st.mon == 7].y) ** 2))
        print(f"{label:22s} all {mse ** 0.5:6.1f}  stable {(smse / sw) ** 0.5:6.1f}  "
              f"Jan {jan:6.1f}  Jul {jul:6.1f}  | " +
              " ".join(f"{a} {r:.0f}" for a, r in parts.items()))

    base = valb["base_pred"].to_numpy()
    score(base, "base")
    for w in (0.5, 1.0):
        score(base + w * delta, f"base + {w} x corrector")
    imp = pd.Series(corr.feature_importance("gain"), index=feats).sort_values(ascending=False)
    print("corrector top features:", (imp / imp.sum() * 100).head(8).round(1).to_dict())


if __name__ == "__main__":
    main()
