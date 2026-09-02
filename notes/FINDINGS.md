# Data findings

## Schema (ranking.parquet / training)

Movement side (`_mvt`): `MVT_ID_mvt`, `FLIGHT_ID_mvt`, `FLIGHT_mvt`,
`FLIGHT_RULE_mvt`, `ADEP_mvt`, `ADES_mvt`, `PHASE_mvt` (ARR/DEP),
`MVT_TIME_UTC_mvt` (takeoff for DEP), `BLOCK_TIME_UTC_mvt` (off-block),
`SCHED_TIME_UTC_mvt`, `AIRCRAFT_TYPE_mvt`, `RUNWAY_mvt`, `STAND_mvt`,
`TAXITIME_SEC_mvt` (target).

Flight side (`_flt`): `LOBT_flt`, `CALLSIGN_flt`, `ADEP_flt`, `ADES_flt`,
`ADES_FILED_flt`, `MARKET_SEGMENT_flt`, `IOBT_flt`, `FLIGHT_RULE_flt`,
`FLIGHT_TYPE_flt`, `AIRCRAFT_TYPE_flt`, `WK_TBL_CAT_flt` (wake cat),
`AIRCRAFT_OPERATOR_flt`, `EOBT_1_flt`, `ARVT_1_flt`, `AOBT_3_flt`,
`ARVT_3_flt`.

Note the phase column is `PHASE_mvt`, not `PHASE`.

## Target definition (confirmed on training, 100% exact)

    TAXITIME_SEC_mvt == epoch(MVT_TIME_UTC_mvt - BLOCK_TIME_UTC_mvt)

i.e. takeoff time minus actual off-block time, in seconds.

## The AOBT_3_flt leakage question — RESOLVED: not a clean leak

In ranking.parquet, for the ~215,876 PHASE=DEP rows:
  - `TAXITIME_SEC_mvt`   : 0 filled (blanked, as stated)
  - `BLOCK_TIME_UTC_mvt` : 0 filled (blanked, as stated)
  - `MVT_TIME_UTC_mvt`   : 100% filled (takeoff NOT blanked)
  - `AOBT_3_flt`         : 98.5% filled (212,677 rows)

So one can compute `recov = MVT_TIME_UTC_mvt - AOBT_3_flt` as a proxy for
taxi-out. But AOBT_3_flt (Network Manager off-block) is NOT the same
instant as the movement's true off-block:

  - `AOBT_3_flt - BLOCK_TIME_UTC_mvt`: median -60s, IQR [-224, +112]s;
    only 10% within 30s.
  - `recov` vs true taxi-out (training Jan 2025): **RMSE 377s**, median
    abs err 176s, 21% within 60s, 0.7% exact. Trimming to plausible
    range (60..3000s) only drops RMSE to 351s — the error is intrinsic,
    not outliers.

Conclusion: the blanking is effective. `recov`/AOBT_3_flt is a useful
**feature** (standalone ~360s RMSE, similar to a naive median baseline),
not a recovered answer. It appears in the training data too, so it is
clearly intended to be usable. Still worth a quick Discord confirmation
before leaning on it, but there is no ranking-process exploit here.

## Baseline implication

A median-by-group baseline and an AOBT-based proxy are in the same ~350s
ballpark. Real gains will come from combining AOBT_3_flt (and EOBT_1_flt)
as features with congestion/queue features in a per-airport model, not
from either signal alone.

## Submission log

| ver | approach | offline estimate | actual score |
|-----|----------|------------------|--------------|
| v1  | per-airport hybrid: hierarchical group mean of taxitime (direct) vs `recov + mean(offset)`; offset used at EDDF, LFPG, LIRF, LSZH; clipped [60, 7200] | 505.9s | **511.88s** |

The ranking-weighted offline estimate came within 1.2% of the real score,
so the validation harness (fit on 10 months, score Jan+Jul 2025, weight
per-airport RMSE by the ranking set's airport mix) is reliable enough to
iterate against without burning submissions.

Scoring detail: results land in the team bucket as
`<file>_result.json` within ~15s, containing `score` and `used_pairs`.
Truth set is `prc-2026-testsets/truthing.parquet`.

## Airports: 10, not 11

LTAI (Antalya) does not appear in the training or ranking data at all.
Present: EDDF EDDM EGLL EHAM LEBL LEMD LFPG LIRF LSZH LTFM.

## The LIRF scheduled-time fallback (the big one)

`BLOCK_TIME_UTC_mvt` is not always a captured pushback event. On a subset
of movements it falls back to `SCHED_TIME_UTC_mvt`, so the target becomes

    taxitime == takeoff - SCHED_TIME_UTC_mvt  ==  D

and D is directly observable in the ranking set. Evidence:

  - LIRF flights >2h: 428 of 480 (89%) have |BLOCK - SCHED| <= 60s, and
    median (BLOCK - SCHED) is -1s while (BLOCK - EOBT_1) is -3961s.
  - Those 480 flights are 0.3% of LIRF rows but 81.5% of its squared
    error; LIRF is ~40% of the whole competition's error.

**Discriminator**: movements with no matched Network Manager flight
record (`AOBT_3_flt`/`EOBT_1_flt`/`LOBT_flt`/`IOBT_flt` all NULL together)
are 54.8% artifact vs 2.9% when matched — a ~19x lift. Mechanically
sensible: an unreconciled movement is exactly the one lacking a real
off-block, so it falls back to schedule.

**Scope**: this is essentially LIRF-only (49.2% artifact rate among
unmatched high-delay flights, vs ~0% at every other airport; LTFM 1.5%).
So the p-model must NOT pool across airports — doing so leaked LIRF's
rate into EDDF/EHAM and cost ~45s. Fallback hierarchy is
(apt, unmatched, D-bin) -> (apt, unmatched) -> 0.

Model (v4): the target is a mixture, so predict the mixture mean

    pred = p * D + (1 - p) * normal(apt, stand, rwy)

with `normal` fitted on non-artifact rows only.

## Submission log (updated)

| ver | approach | offline estimate | actual score |
|-----|----------|------------------|--------------|
| v1  | per-airport direct/offset hybrid, group means | 505.9s | **511.88s** |
| v2  | v4 model: scheduled-fallback mixture, p by (apt, unmatched, D-bin) | 420.1s | **458.24s** |

Caveat on the harness: it predicted v1 within 1.2% but v2 only within
8.3%. The v2 gain rides on rare extreme events (0.4% of LIRF rows), whose
rate evidently differs between 2025 training and the 2026 truth set. So
the harness stays reliable for broad changes but *overstates* gains that
come from the extreme tail — treat tail-driven estimates as optimistic.
