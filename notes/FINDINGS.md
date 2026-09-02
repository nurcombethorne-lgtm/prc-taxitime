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

## Ranking set composition (fixes the validation harness)

The ranking set is NOT a random sample — it is complete for the
airport-months it covers:

  - Jan 2026: all ten airports (152,650 departures)
  - Jul 2026: **EDDF, EGLL and EHAM only** (63,152 departures)
  - a handful of spillover rows in Feb/Aug (take-offs just past midnight)

So seven of the ten airports are scored on **January only**. Validating
them on Jan+Jul understates their difficulty: LFPG scores 810s on
January alone versus 569s averaged over Jan+Jul, because winter (de-icing)
is the hard case and the leaderboard only ever sees LFPG in winter. This
mismatch explains v2's optimistic estimate.

The harness now scores each airport on the months it actually appears in,
weighted by its share of ranking rows.

Because the ranking set is complete per airport-month, per-airport
counting features (queues, throughput) are not deflated by sampling and
are valid to use.

## Queue / congestion features

`dep_queue` = aircraft off-block but not yet airborne at this aircraft's
pushback, built as a running +1/-1 balance over an event stream.

Trap: only flights with BOTH a pushback and a take-off may enter the
stream. Including take-offs whose AOBT_3_flt is missing adds a -1 with no
matching +1, and that imbalance accumulates into a large negative drift
over the year (mean dep_queue of -971 instead of ~10). After the fix the
train and ranking distributions agree closely (e.g. EDDF 10.2 vs 10.1)
and mean taxi rises monotonically from 872s (queue 0-2) to 1234s (25+).

## Submission log (updated)

| ver | approach | offline estimate | actual score |
|-----|----------|------------------|--------------|
| v1  | per-airport direct/offset hybrid, group means | 505.9s | **511.88s** |
| v2  | scheduled-fallback mixture, p by (apt, unmatched, D-bin) | 420.1s | **458.24s** |
| v3  | LightGBM over congestion features as the `normal` term, corrected harness | 354.9s | **314.42s** |

Feature gain in v3: recov 33.5%, aobt_vs_eobt 19.8%, rwy 10.8%,
stand 10.6%, ades 4.6%, sched_dep_60 4.3%, operator 3.7%. The queue and
throughput features contribute far less than the AOBT-derived timing
features — congestion matters, but knowing when the aircraft actually
pushed back matters much more.

Harness calibration is now conservative rather than optimistic (v3 scored
40s BETTER than estimated), consistent with January 2025 being a harder
month than January 2026.

## Winter-operations hypothesis: WRONG

LFPG and LIRF being the worst airports, and both being January-only on the
leaderboard, suggested de-icing / winter operations. Residual analysis
refutes it. The error is not a broad seasonal effect but a handful of
corrupted individual flights:

  - LFPG January: the **top 10 flights carry 88.4% of squared error**, and
    two days (19 and 23 Jan) carry 87.5%. Worst single flight has
    y = 84,240s (23 hours).
  - LIRF January: top 10 flights = 71.3%; one day (25 Jan) = 61.4%.

Across a whole year LFPG has only ~4 flights over 4 hours (0.1% of its
3,762 unmatched rows). Our January validation happened to contain two of
them. So LFPG's ~810s RMSE is set by a couple of freak records, the count
of which in the 2026 truth is essentially a lottery. **Chasing it is
chasing noise**, and any per-airport LFPG estimate is very high variance.

The two airports also fail in opposite directions, which is worth knowing:
LFPG is dominated by huge under-predictions (genuine extreme values we
cannot see coming), LIRF by over-predictions - false positives of the
scheduled-fallback rule, where p is high but the flight turned out normal.
The mixture is nonetheless well calibrated in the mean per (apt, unmatched,
D-bin) cell, so those errors are largely irreducible given the features.

## Things tried that did NOT work

  - **Contemporaneous "nowcast" features.** Arrival taxi-IN times are not
    blanked in the ranking set, so mean arrival taxi-in and mean departure
    `recov` over the preceding hour are legitimately available at predict
    time. Distributions match between train and ranking (arrival taxi-in
    512.2s vs 510.1s), but adding them moved the estimate 349.3 -> 349.6,
    i.e. nothing. The flight's own `recov` already carries the signal.
    Kept in features.py, left out of the model.
  - **Offset reformulation via group means** (see experiment_offset.py):
    lost overall, because stand/runway explain taxi *distance* well but
    explain pushback delay poorly.

## What did work: residual target

`recov` is the strongest feature, but a tree cannot represent
`y = recov + correction`, since its leaves emit constants. Training the
matched-flight model on the residual `y - recov` and adding `recov` back
is a much easier target. Unmatched flights (no `recov`) get a separate
model on the raw target.

## Submission log (final for this session)

| ver | approach | offline estimate | actual score |
|-----|----------|------------------|--------------|
| v1  | per-airport direct/offset hybrid, group means | 505.9s | **511.88s** |
| v2  | scheduled-fallback mixture, p by (apt, unmatched, D-bin) | 420.1s | **458.24s** |
| v3  | LightGBM over congestion features, corrected harness | 354.9s | **314.42s** |
| v4  | + `unmatched`/`D` as features, residual target `y - recov` | 346.9s | **300.48s** |

Remaining error is concentrated in LFPG (~45% of weighted MSE) and LIRF
(~18%), both dominated by irreducible corrupted records.

## The 24-hour block-time fault (corroborated on Discord)

A competitor reported anomalies; the counts reproduce exactly on our copy,
so the report is sound:

  - 119 training rows with taxi time > 300 min (91 DEP + 28 ARR) — exact match
  - ~15 DEP rows within an hour of exactly 24h, target = 86400 + a normal taxi
  - EHAM January 2026 has 805 rows with the `_flt` family null — exact match

Cause appears to be BLOCK_TIME landing a day early (or MVT_TIME a day late).
Our own worst residuals are this fault: LFPG y=84,240 and LIRF y=87,177.

**Not exploitable.** For roughly half of them every ranking-visible column
looks completely ordinary (LSZH: D=939s, recov normal, truth 87,341s), so
there is no signature to key on. The other half coincide with the
scheduled-fallback artifact (y == D) and are already handled.

**But it dominates the scoring floor.** At a rate of 15/2,085,047, about
1.55 such rows are expected among the 215,876 scored departures, and a
single missed one contributes ~33,800 to MSE — 37% of our MSE at RMSE 300.
The irreducible variance p(1-p)*86400^2 is ~53,700, roughly 59% of it. So
a large share of every team's score is a lottery on one or two corrupted
rows, identically for everyone since the truth set is shared. The
RMSE-optimal hedge is only p*86400 = 0.62s per row, i.e. not worth adding.

**What it did surface:** our prediction ceiling was set at 40,000s, which
truncated legitimate high-D predictions. Lifting it improved validation
346.9 -> 342.8s (saturating by 60,000) and the leaderboard 300.48 -> 297.01.

| ver | approach | offline estimate | actual score |
|-----|----------|------------------|--------------|
| v5  | as v4 with the prediction ceiling raised to 90,000s | 342.8s | **297.01s** |

Worth watching: if the organisers regenerate or filter the dataset, scores
move for everyone — and whether existing submissions are rescored or must
be resubmitted is still unanswered.

## Hyperparameter tuning does NOT transfer — the harness has a noise floor

v6 tuned the boosting rounds on the validation curve (matched model peaks
at ~400 rounds, unmatched at ~800) and averaged three seeds. Validation
improved 342.8 -> 338.6s, a clean 4.2s. The leaderboard went the other
way: 297.01 -> 297.24.

The gain was selection bias: rounds and seeds were chosen against the very
set used to measure them, so a few seconds of apparent improvement was
fitting the validation noise rather than the problem.

**Rule going forward: treat differences below roughly 5s on this harness as
noise, and only submit changes with a clear mechanism.** Every change that
actually transferred had one:

| change | mechanism | transferred? |
|---|---|---|
| v2 scheduled-fallback mixture | target is mechanically `D` for a known subgroup | yes, -53s |
| v3 GBM over features | genuinely new information | yes, -144s |
| v4 residual target `y - recov` | trees cannot represent `recov + correction` | yes, -14s |
| v5 raised prediction ceiling | clip was truncating valid predictions | yes, -3.5s |
| v6 rounds + seed averaging | no mechanism, pure tuning | **no, +0.2s** |

Best score stands at **297.01 (v5)** since best-of-all-submissions counts,
so the failed experiment cost nothing but is worth not repeating.

## Leaderboard context (2 Sep 2026)

8th of 17. Leader quick-boat 253.94 (14 versions), 2nd enthusiastic-daisy
257.50 (51 versions), 3rd jovial-uniform 259.67. Podium is ~37s away and
9th place is 0.07s behind us.

Critically, the leader's 253.94 proves the earlier "we are near the
irreducible floor" reading was wrong: that estimate came from the
*expected* count of 24h-fault rows, which has enormous variance. At least
43s of real signal remains to be found — by structural work, not tuning.

## Turnaround linkage — and a bug it exposed

**The bug first.** `features.py` derived the movement airport as `ADEP_mvt`
for every row. That is only correct for departures: an ARRIVAL's movement
happens at `ADES_mvt`, while `ADEP_mvt` is then its *origin*. Verified:
100% of arrival rows have `ADES_mvt` among the ten airports, and only 16%
have `ADEP_mvt` among them. So every arrival-derived feature had been
bucketing arrivals under foreign origin airports — pure noise. That is the
real reason the earlier "nowcast" experiment showed exactly zero gain; the
idea was sound, the plumbing was broken.

**The linkage.** Arrivals carry an on-block time that is not blanked in the
ranking set, so the inbound leg that delivered an aircraft is recoverable
as the most recent arrival that went on-block at the same stand before this
departure pushes back (no registration field exists, so stand+time
adjacency is the join; matches older than 24h are discarded). Coverage is
98.0% in training and 97.3% in ranking, median turnaround ~92 vs ~99 min —
distributions agree, so the feature transfers.

New features: `turnaround_sec`, `inbound_delay`, `inbound_taxi_in`, plus
the now-meaningful `arr_taxi_mean60` / `dep_recov_mean60`.

**Ablation (this is the disciplined part).** Adding them everywhere looked
*worse* on the all-airport metric (339.4 -> 341.0), but that was LIRF alone
regressing +48.3 while **8 of 8 stable airports improved**:

    EGLL -7.5  LEBL -5.3  LTFM -5.1  EDDF -2.5  EDDM -2.2
    LSZH -2.1  LEMD -1.8  EHAM +0.1        stable-only 226.6 -> 222.9

Consistency across 8/8 is signal; the LIRF swing is the known lottery. The
cause: unmatched flights have no AOBT_3_flt, so the turnaround reference
falls back to scheduled time and the features are unreliable exactly where
LIRF's error lives. Giving the **matched** model the new features and
leaving the **unmatched** model on the base set keeps the gain and removes
the regression:

| variant | all-airport | stable-only | LIRF |
|---|---|---|---|
| base | 339.4 | 226.6 | 552.0 |
| new features everywhere | 341.0 | 222.9 | 600.3 |
| **new features, matched model only** | **336.8** | **223.0** | **550.5** |

**Lesson: judge features on the stable airports.** The all-airport metric is
dominated by LIRF/LFPG variance and would have caused us to reject a change
that genuinely helps everywhere else.

| ver | approach | offline estimate | actual score |
|-----|----------|------------------|--------------|
| v7  | ADES_mvt fix + turnaround linkage, matched model only | 336.2s | **292.22s** |
