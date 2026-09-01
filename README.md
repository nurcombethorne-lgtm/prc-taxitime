# PRC Data Challenge 2026 — taxi-out time prediction

Team **resilient-kiwi** — WPTG.ai (White Pearl Technology Group AB, Sweden).

Predicts taxi-out time (seconds) for departures at 11 European airports
(EDDF EDDM EGLL EHAM LEBL LEMD LFPG LIRF LTAI LTFM LSZH), scored on RMSE
against January and July 2026 movements.

Licensed under **GPLv3** (see `LICENSE`), as required by the challenge
prize conditions. All external data used is open and documented below.

## Data

All competition data comes from the OpenSky Network MinIO store at
`https://s3.opensky-network.org` (participant credentials required):

- `training_2025-MM-01_*.parquet` — 12 monthly files, 4,167,797 movements
- `ranking.parquet` — Jan + Jul 2026 movements; `BLOCK_TIME_UTC_mvt` and
  `TAXITIME_SEC_mvt` blanked for `PHASE=DEP`
- `submitting.parquet` — submission template (`MVT_ID_mvt`, `TAXITIME_SEC_mvt`)

Column suffixes: `_mvt` = airport movement records, `_flt` = Network
Manager flight list. The data is messy and unreconciled by design.

No other external datasets are currently used. Any added later will be
listed here with source and licence.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # then fill in your MinIO access key (see comments)
uv run scripts/fetch_data.py --discover   # find the data bucket
uv run scripts/fetch_data.py --bucket <data-bucket>
```

Get the access key from the MinIO console at
**https://s3-console.opensky-network.org** (Other Authentication Methods →
Login with SSO → Access Keys → Create access key). Note this is a
different host from `s3.opensky-network.org:9443`, which is unreachable;
`scripts/sts_login.py` is a token-based fallback kept only for the case
where the console itself is down.

## Workflow

```bash
uv run scripts/inspect_ranking.py                 # schema / leakage audit of ranking.parquet
uv run scripts/baseline.py                        # fit + validate + write submissions/resilient-kiwi_vN.parquet
uv run scripts/validate_submission.py submissions/resilient-kiwi_v1.parquet
uv run scripts/upload_submission.py submissions/resilient-kiwi_v1.parquet
```

`validate_submission.py` enforces the organisers' constraints (exact
`MVT_ID_mvt` match, no missing/extra/duplicate rows, no nulls) before any
upload; `upload_submission.py` refuses to upload unless it passes.

## Approach

1. **Baseline (week 1):** hierarchical median taxi-out by
   airport × stand × runway with fallback to airport × runway → airport →
   global. Locks in a score early — best submission counts, not the last.
2. **Unimpeded taxi time:** empirical unimpeded reference per
   airport/stand/runway, mirroring the PRC "additional taxi-out time"
   methodology.
3. **Congestion features:** departure queue size — aircraft off-block and
   not yet airborne at the same airport in the preceding window.
4. **Per-airport models** — Antalya and Zurich are different problems.
5. **Validation on Jan + Jul 2025** specifically (the ranking months'
   seasons), never a random split.
