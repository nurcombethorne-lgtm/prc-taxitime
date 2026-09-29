"""Does the model use a trace at an airport where it never saw one in training?

On the 2026 scored set EGLL and LEMD are 60-98% traced; in 2025 they were
almost untraced. Simulation: for a well-covered airport A, blank A's trace
features in the fit months (as if A had no 2025 coverage), then score A's
traced January/July flights with

  current   the live architecture (all categoricals, residual on recov)
  agnostic  a model fitted only on traced rows of the OTHER airports, with
            no airport / stand / runway / operator / destination identity
  upper     the live architecture with A's traces left in (what we could
            reach if 2025 had covered A)

Matched, non-fallback rows only; RMSE of the `normal` prediction.

    uv run scripts/experiment_transfer.py
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

AIRPORTS = ("EHAM", "EDDM", "LSZH")
AGN_CATS = ["actype", "wake", "segment"]


def rmse(a, b) -> float:
    return float(np.sqrt(np.mean((np.asarray(a) - np.asarray(b)) ** 2)))


def main() -> None:
    M.enable_groups("adsb,adsb_pos")
    trace = [f for f in M.FEATS if f.startswith("adsb_") and f != "adsb_day_covered"]
    con = duckdb.connect(str(M.DB), read_only=True)
    df = con.sql("""SELECT t.*, w.* EXCLUDE (mvt_id), b.* EXCLUDE (mvt_id) FROM train_feat t
                    LEFT JOIN wx w USING (mvt_id) LEFT JOIN adsb b USING (mvt_id)
                    WHERE t.y IS NOT NULL AND t.y > 0 AND t.recov IS NOT NULL""").df()
    con.close()
    df = df[~(df["D"].notna() & (df["y"] - df["D"]).abs().le(M.TOL))]
    vm = df["mon"].isin([1, 7])
    fit, val = M.prep(df[~vm].copy()), M.prep(df[vm].copy())
    prm = dict(M.PARAMS, seed=1, bagging_seed=1, feature_fraction_seed=1)
    agn_feats = AGN_CATS + [f for f in M.NUMS if f in fit.columns]

    print("fitting upper-bound model (all traces in) ...", flush=True)
    upper = lgb.train(prm, lgb.Dataset(fit[M.FEATS], fit["y"] - fit["recov"],
                                       categorical_feature=M.CATS), num_boost_round=M.NUM_ROUNDS)
    rows = []
    for A in AIRPORTS:
        print(f"airport {A} ...", flush=True)
        f2 = fit.copy()
        isA = (f2["apt"] == A).to_numpy()
        f2.loc[isA, trace] = np.nan
        f2.loc[isA, "adsb_day_covered"] = 0
        cur = lgb.train(prm, lgb.Dataset(f2[M.FEATS], f2["y"] - f2["recov"],
                                         categorical_feature=M.CATS), num_boost_round=M.NUM_ROUNDS)
        tr = fit[(fit["apt"] != A) & (fit["adsb_present"] == 1)]
        agn = lgb.train(dict(prm, num_leaves=127), lgb.Dataset(tr[agn_feats], tr["y"] - tr["recov"],
                                                             categorical_feature=AGN_CATS),
                        num_boost_round=M.NUM_ROUNDS)
        v = val[(val["apt"] == A) & (val["adsb_present"] == 1)]
        rec = v["recov"].to_numpy()
        pc = cur.predict(v[M.FEATS]) + rec
        pa = agn.predict(v[agn_feats]) + rec
        pu = upper.predict(v[M.FEATS]) + rec
        vb = v.copy(); vb[trace] = np.nan; vb["adsb_day_covered"] = 0
        pn = cur.predict(vb[M.FEATS]) + rec          # the same model with the trace withheld
        rows.append(dict(apt=A, n=len(v), no_trace=rmse(pn, v.y), current=rmse(pc, v.y),
                         agnostic=rmse(pa, v.y), blend=rmse((pc + pa) / 2, v.y), upper=rmse(pu, v.y),
                         recov_only=rmse(rec, v.y)))
    out = pd.DataFrame(rows).round(1)
    print(out.to_string(index=False))
    print("TRANSFER_DONE")


if __name__ == "__main__":
    main()
