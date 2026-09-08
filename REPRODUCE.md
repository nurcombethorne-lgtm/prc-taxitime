# Reproducing the submission

This reproduces `resilient-kiwi_v14.parquet`, our best scoring submission
(RMSE **296.47** on the regenerated 344,841-row ranking set issued on
4 Sep 2026). Scores from before that re-issue (v1-v8, best 291.59) were
against a smaller, easier test set and are not comparable.

## 1. Environment

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync
```

On macOS LightGBM additionally needs OpenMP, or importing it fails with
`Library not loaded: @rpath/libomp.dylib`:

```bash
brew install libomp
```

## 2. Credentials

Competition data lives in the OpenSky MinIO store and needs a participant
account. Create an access key in the MinIO console at
**https://s3-console.opensky-network.org** — "Other Authentication
Methods" → "Login with SSO" → Access Keys → Create access key. (Note this
host, not `s3.opensky-network.org:9443`, which is unreachable.)

```bash
cp .env.example .env      # then fill in the key pair
```

`.env` is gitignored and contains no committed values.

## 3. Data

```bash
uv run scripts/fetch_data.py --discover                 # locate the bucket
uv run scripts/fetch_data.py --bucket prc-2026-datasets # ~304 MB
```

Downloads twelve monthly `training_*.parquet` files (2,085,047
departures), `ranking.parquet` and `submitting.parquet` into `data/`.
Re-runs skip files already present at the right size.

## 4. Build features, train, submit

```bash
uv run scripts/features.py                    # ~5 min -> data/features.duckdb
uv run scripts/fetch_weather.py               # METAR history, ~16 MB -> data/weather/
uv run scripts/weather_features.py            # joins weather onto features.duckdb
uv run scripts/model_gbm.py --dry-run         # validation only
uv run scripts/model_gbm.py                   # + writes submissions/resilient-kiwi_vN.parquet
uv run scripts/validate_submission.py submissions/resilient-kiwi_vN.parquet
uv run scripts/upload_submission.py submissions/resilient-kiwi_vN.parquet
```

`model_gbm.py` prints a per-airport table, a ranking-weighted estimate,
and a **stable-only** estimate that excludes LIRF and LFPG. Judge changes
by the stable figure: the headline number is dominated by a handful of
corrupted records at those two airports and swings on luck (see
`notes/FINDINGS.md`).

The submission version number is taken from the count of existing files
in `submissions/`, so remove or renumber them to control the output name.
`upload_submission.py` refuses to upload anything the validator rejects.

## 5. What the model does

Target identity, exact in 100% of training rows:

    TAXITIME_SEC_mvt == MVT_TIME_UTC_mvt - BLOCK_TIME_UTC_mvt

`BLOCK_TIME_UTC_mvt` is blanked for departures in the ranking set, but
`MVT_TIME_UTC_mvt` (take-off) is not. On a subset of movements the block
time is not a captured pushback at all but a fallback to
`SCHED_TIME_UTC_mvt`, which makes the target exactly

    D := MVT_TIME_UTC_mvt - SCHED_TIME_UTC_mvt

and D is observable. The target is therefore a two-component mixture, and
the RMSE-optimal prediction is its mean:

    pred = p * D + (1 - p) * normal

* **p** — a LightGBM binary classifier for P(target == D), trained on all
  rows with a D. Near-perfectly calibrated on validation, which is the
  property the mixture needs.
* **normal** — the ordinary taxi time. Two boosters: flights with a
  Network Manager record are trained on the **residual** `y - recov`
  (where `recov = take-off - AOBT_3_flt`) with `recov` added back, since a
  tree cannot represent `recov + correction` from constant leaves;
  unmatched flights have no `recov` and get their own model on the raw
  target.

* **Weather** — METAR observations from the Iowa Environmental Mesonet
  archive (open), ASOF-joined at each airport within 90 minutes:
  temperature, visibility, wind, precipitation, and flags for de-icing
  risk, low visibility, snow and freezing, plus cumulative measures
  (precipitation over 6h/12h, fraction of the last 6h in de-icing
  conditions, hours since the airfield was last above freezing). De-icing
  weather is far more common in the 2026 ranking months than in the 2025
  validation months, which is why these transfer better than validation
  suggests.
* **Flight-plan revision gaps** — `AOBT_3 − LOBT` and `AOBT_3 − IOBT`
  per flight (how far NM's actual moved from its last and initial
  off-block estimates), plus the mean `AOBT_3 − EOBT_1` over the previous
  hour's other departures at the same airport as an operational-state
  nowcast. These target the gap between NM's and the airport's off-block
  stamps, which is most of the matched-flight error and is clustered
  within the hour.
* **LIRF fallback rule** — for LIRF departures with no NM record whose
  take-off is 6h or more past schedule, predict `D` outright
  (`FORCE_D_ENABLED`). In that subgroup the target is almost never a
  normal taxi (1 of 63 training rows); it is the scheduled-time fallback,
  which the unblanked 2026 arrivals show surviving at the 2025 rate.

Predictions are clipped to [60, 90000] seconds — the ceiling accommodates
a known data fault that puts block time a day early, yielding genuine
targets just above 86,400 s.

Features come from `features.py`: departure and arrival queues (running
+1/-1 balances over event streams), recent throughput, scheduled demand,
aircraft turnaround linked via stand and on-block adjacency, and the
AOBT-derived timing columns that dominate the model.

## 6. Validation

Fit on ten months of 2025, score January and July 2025. Each airport is
scored **only on the months it appears in within the ranking set**, then
weighted by its share of ranking rows. Both are read from the ranking file
itself, so the harness adapts to re-issues without code changes: the
original extract had July for EDDF, EGLL and EHAM only, and the 4 Sep 2026
re-issue has July for all ten airports at 56% of the set.

Two caveats the notes explain in full. The headline estimate swings on a
handful of corrupted records at LIRF and LFPG, so judge changes by the
stable-only figure. And when a feature's driving condition occurs at a
different rate in the validation months than in the scored months (as
de-icing weather does), the raw validation gain misestimates transfer —
check prevalence in both.

## 7. Script inventory

**Careful: the `baseline_vN.py` numbers are model iterations and do NOT
correspond to submission version numbers.**

Live pipeline:

| script | role |
|---|---|
| `s3util.py` | S3/MinIO client and paths, reads `.env` |
| `fetch_data.py` | download the datasets |
| `features.py` | build `data/features.duckdb` |
| `fetch_weather.py` | download METAR history for the ten airports (IEM archive) |
| `weather_features.py` | join weather onto the feature set (`wx` table) |
| `model_gbm.py` | **the model** — produced submissions v3 through v14 |
| `check_dataset.py` | detect a re-issued competition dataset |
| `leaderboard.py` | read standings from the API (the public notebook is broken) |
| `validate_submission.py` | enforce the organisers' row constraints |
| `upload_submission.py` | validator-gated upload |

Kept for the record, not part of the pipeline:

| script | what it was |
|---|---|
| `inspect_ranking.py` | audit of whether taxi-out is recoverable from `AOBT_3_flt` (it is not) |
| `baseline.py` | hierarchical median by airport × stand × runway |
| `baseline_v2.py` | per-airport direct/offset hybrid — produced submission **v1** |
| `baseline_v3.py` | first scheduled-fallback mixture |
| `baseline_v4.py` | mixture conditioned on NM-match status — produced submission **v2** |
| `experiment_baseline.py` | median vs mean vs winsorised group aggregates |
| `experiment_offset.py` | predicting the offset instead of taxi time (lost) |
| `experiment_ensemble.py` | CatBoost / raw-target blend of the `normal` term (lost; see notes) |
| `experiment_reporting_gap.py` | the diagnostics that found the LOBT/IOBT features behind v14 |
| `sts_login.py` | token-based credential fallback, for when the console is down |

`notes/FINDINGS.md` records every result including the negative ones, and
`notes/PLAN.md` the original approach.
