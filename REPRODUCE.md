# Reproducing the submission

This reproduces `resilient-kiwi_v8.parquet`, our best scoring submission
(RMSE **291.59** on the challenge leaderboard).

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

Predictions are clipped to [60, 90000] seconds — the ceiling accommodates
a known data fault that puts block time a day early, yielding genuine
targets just above 86,400 s.

Features come from `features.py`: departure and arrival queues (running
+1/-1 balances over event streams), recent throughput, scheduled demand,
aircraft turnaround linked via stand and on-block adjacency, and the
AOBT-derived timing columns that dominate the model.

## 6. Validation

Fit on ten months of 2025, score January and July 2025. Each airport is
scored **only on the months it appears in within the ranking set**
(January 2026 covers all ten airports; July 2026 covers only EDDF, EGLL
and EHAM), then weighted by its share of ranking rows. Scoring every
airport on both months flatters the seven that the leaderboard sees only
in winter.

## 7. Script inventory

**Careful: the `baseline_vN.py` numbers are model iterations and do NOT
correspond to submission version numbers.**

Live pipeline:

| script | role |
|---|---|
| `s3util.py` | S3/MinIO client and paths, reads `.env` |
| `fetch_data.py` | download the datasets |
| `features.py` | build `data/features.duckdb` |
| `model_gbm.py` | **the model** — produced submissions v3 through v8 |
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
| `sts_login.py` | token-based credential fallback, for when the console is down |

`notes/FINDINGS.md` records every result including the negative ones, and
`notes/PLAN.md` the original approach.
