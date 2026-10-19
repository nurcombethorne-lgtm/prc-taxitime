"""Score-trajectory figure for the JOAS paper (figures/trajectory.pdf)."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from pathlib import Path

V = [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
S = [321.89, 318.06, 316.80, 319.70, 301.70, 296.47, 296.56, 285.75, 284.56, 277.18, 272.81, 271.29]
LAB = {9: "re-issued set", 10: "METAR", 11: "de-icing backlog", 12: "day-fault rule (failed)",
       13: "force fallback at LIRF", 14: "plan-revision gaps", 15: "drop month (null)",
       16: "ADS-B traces", 17: "more trace days", 18: "all Jan/Jul traces + stand distance",
       19: "out-of-fold corrector", 20: "full-year traces"}
best = [min(S[:i + 1]) for i in range(len(S))]
fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=200)
ax.step(V, best, where="post", color="#333", lw=1.2, label="best so far")
ax.plot(V, S, "o", color="#0b6", ms=5, label="submission")
for x, y in zip(V, S):
    ax.annotate(LAB[x], (x, y), xytext=(0, 7 if x not in (12, 15) else -12),
                textcoords="offset points", ha="center", fontsize=6.2, color="#333")
ax.set_xlabel("submission"); ax.set_ylabel("public-board RMSE (s)")
ax.set_xticks(V); ax.set_ylim(262, 328)
ax.spines[["top", "right"]].set_visible(False)
ax.legend(frameon=False, fontsize=7, loc="upper right")
fig.tight_layout()
out = Path(__file__).resolve().parents[1] / "paper" / "figures" / "trajectory.pdf"
fig.savefig(out); print(f"wrote {out}")
