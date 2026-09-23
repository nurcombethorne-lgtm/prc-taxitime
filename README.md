# PRC Data Challenge 2026 — taxi-out time prediction

Team **resilient-kiwi** — WPTG.ai (White Pearl Technology Group AB, Sweden).

Predicts taxi-out time (seconds) for departures at ten European airports
(EDDF EDDM EGLL EHAM LEBL LEMD LFPG LIRF LSZH LTFM), scored on RMSE
against January and July 2026 movements. The challenge documentation lists
an eleventh, LTAI (Antalya), but it does not appear as a reporting airport
in any of the data (see `notes/FINDINGS.md`).

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

### External data

**METAR observations** for the ten airports, 2025-01-01 to 2026-08-01,
from the Iowa Environmental Mesonet ASOS/METAR archive (Iowa State
University, https://mesonet.agron.iastate.edu/request/download.phtml) —
an open, freely redistributable archive of routine aerodrome weather
reports. Fetched by `scripts/fetch_weather.py` into `data/weather/`
(gitignored, ~16 MB).

**ADS-B ground traces** from the adsb.lol open archive
(https://github.com/adsblol/globe_history_2026, one release per day,
Open Database License / CC0). Only ground segments at the ten airports
are kept (`scripts/adsb_extract.py`), one small parquet per day in
`data/adsb/segments/` (gitignored). The organisers ruled on 19 Sep 2026
that open trajectory archives may be used to extract off-block times if
declared. No OpenSky state-vector data is used; the organisers ruled it
inadmissible on 3 Sep 2026.

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

Full reproduction instructions, the model description and a script
inventory are in **[REPRODUCE.md](REPRODUCE.md)**; every result including
the negative ones is recorded in [notes/FINDINGS.md](notes/FINDINGS.md).

## Workflow

```bash
uv run scripts/features.py                    # build data/features.duckdb
uv run scripts/fetch_weather.py               # METAR history -> data/weather/
uv run scripts/weather_features.py            # join weather onto features.duckdb
uv run scripts/model_gbm.py --dry-run         # validate only
uv run scripts/model_gbm.py                   # + write submissions/resilient-kiwi_vN.parquet
uv run scripts/validate_submission.py submissions/resilient-kiwi_v14.parquet
uv run scripts/upload_submission.py submissions/resilient-kiwi_v14.parquet
```

`model_gbm.py` is the live model. The `baseline*.py` scripts are earlier
iterations kept for the record — their version numbers are model
iterations and do **not** match submission numbers; see
[REPRODUCE.md](REPRODUCE.md).

`validate_submission.py` enforces the organisers' constraints (exact
`MVT_ID_mvt` match, no missing/extra/duplicate rows, no nulls) before any
upload; `upload_submission.py` refuses to upload unless it passes.

## Approach

The target is exactly `take-off - off-block`, and off-block is blanked for
departures in the ranking set while take-off is not. On a subset of
movements the recorded block time is a fallback to the *scheduled* time
rather than a captured pushback, which makes the target exactly
`take-off - scheduled` — an observable quantity. The target is therefore a
mixture, predicted as

    pred = p * D + (1 - p) * normal

with `p` a calibrated classifier for that regime and `normal` a
gradient-boosted model over congestion, turnaround and timing features.
See [REPRODUCE.md](REPRODUCE.md) for the full description.

Validation fits on ten months of 2025 and scores January and July 2025,
with each airport scored only on the months it actually appears in within
the ranking set, weighted by its share of ranking rows.

### Scores

| ver | change | RMSE |
|-----|--------|------|
| v1 | per-airport direct/offset hybrid over group means | 511.88 |
| v2 | scheduled-fallback mixture | 458.24 |
| v3 | LightGBM over congestion features; corrected validation | 314.42 |
| v4 | residual target `y - recov` | 300.48 |
| v5 | raised the prediction ceiling | 297.01 |
| v6 | hyperparameter tuning (did not transfer) | 297.24 |
| v7 | turnaround linkage + arrival-airport fix | 292.22 |
| v8 | calibrated probability classifier + arrival queue | 291.59 |

The ranking set was re-issued on 4 Sep 2026 (344,841 departures, July now
56% of the set and all ten airports); scores below are on the new set and
are not comparable with those above.

| ver | change | RMSE |
|-----|--------|------|
| v9 | v8 model on the regenerated set | 321.89 |
| v10 | METAR weather features | 318.06 |
| v11 | cumulative de-icing features | 316.80 |
| v12 | LIRF day-fault three-way rule (did not transfer, reverted) | 319.70 |
| v13 | predict the scheduled-time fallback outright on the LIRF subgroup | 301.70 |
| v14 | flight-plan revision gaps (LOBT/IOBT) + airport-state nowcast | 296.47 |
| v15 | v14 without the month feature (year-transfer test; null) | 296.56 |
| v16 | adsb.lol ground traces as a second off-block witness (`--add adsb`) | **285.75** |
