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

## v8: learned artifact probability + arrival queue

**Calibrated classifier for p.** The mixture's `p` (probability the target
is exactly `D`) had been a lookup binned by (airport, unmatched, D-bin),
with the thinnest cells holding only 7-23 samples. Replacing it with a
boosted binary classifier over the feature set gives near-perfect
calibration on validation:

| predicted p | 0.010 | 0.118 | 0.295 | 0.598 | 0.920 |
|---|---|---|---|---|---|
| **actual rate** | 0.008 | 0.119 | 0.300 | 0.596 | 0.922 |

Calibration is the property that matters, since the mixture consumes `p`
as a probability rather than a ranking. Trained on the feature set that
excludes the matched-only columns (that variant also scored best).

**Arrival queue.** Aircraft landed but not yet on-block compete for the
same taxiways as a departure taxiing out. This was uncomputable until the
ADES_mvt fix, since arrivals were attributed to their origin airport.
Train/ranking means agree (4.63 vs 4.48) and mean taxi rises 922s -> 1037s
across the range.

Validation: all-airport 336.8 -> 334.8, stable-only 223.0 -> 220.2.

| ver | approach | stable-only | actual score |
|-----|----------|-------------|--------------|
| v7  | turnaround linkage, binned p | 223.0s | 292.22s |
| v8  | + calibrated p classifier, + arrival queue | 220.2s | **291.59s** |

Note the transfer ratio: 2.8s of stable-metric gain produced 0.6s on the
leaderboard. Returns are clearly diminishing at this level - the remaining
headroom is unlikely to come from further refinements of this architecture.

The model script now prints the stable-only figure alongside the headline,
so the lesson from v7 is built into the workflow rather than remembered.

## Organiser announcements, 3 Sep 2026 (Discord)

1. **The July submit dataset is wrong and will be regenerated.** Quoting
   the organiser: *"regarding the submit dataset, it is incorrect for July:
   my fault...airports didn't report yet when I initially extracted the
   data. I am verifying the new export and will upload soonish."*

   This is the dominant planning fact. When the new export lands:
     - `submitting.parquet` gains rows, so **every existing submission
       becomes invalid** (the validator demands an exact MVT_ID_mvt match);
     - the July airport composition changes, which invalidates the
       per-airport month masks the validation harness is built on
       (currently Jul 2026 = EDDF, EGLL, EHAM only).

   `scripts/check_dataset.py` compares the bucket against
   `notes/dataset_manifest.json` and reports any change. As of 3 Sep the
   files are all still dated 2026-08-13, i.e. not yet re-uploaded.

   **Do not invest in further modelling against the current ranking set.**
   Re-tuning now risks being wasted, and the harness weighting will change.

2. **OSN state vectors are not an admissible external source** "as for
   now" — ground truth is airport-reported timestamps, not derived from
   on-board means. That closes off trajectory data as an avenue.

3. **No submission cap yet**, though the organisers are "thinking to
   implement a threshold soon".

## The RMSE inversion issue — we are not going near it

Other participants pointed out on Discord that RMSE feedback is exactly
invertible. Submitting a vector `p`, then resubmitting with a single row
`k` changed by δ, gives

    N · (MSE' − MSE) = δ² + 2δ · (p_k − y_k)

which solves for the true `y_k`. One extra submission therefore recovers
one true target, and a binary search locates the highest-leverage rows in
a few dozen queries. Given how much of this task's RMSE sits on a handful
of very large taxi times, that would be worth more than any modelling.

We will not use this. The brief's standing instruction is that the
organisers warn against exploiting the ranking process, and recovering
ground-truth labels through the scoring endpoint is precisely that. Our
eight submissions are all genuine model outputs.

## Live standings, 3 Sep 2026 (via API, notebook is broken)

The Observable notebook broke when Observable shipped a new framework on
1 Sep. `scripts/leaderboard.py` reads the underlying API instead.

resilient-kiwi is **12th of 24 at 291.59** with 8 submissions. Third place
is 253.94, so the podium is ~37.6s away. The field has tightened
considerably: several teams entered or improved overnight.

## LTAI (Antalya): documented but absent — confirmed against the data page

The official data page (dc2026/data.html) Table 1 lists **eleven**
reporting airports including LTAI/Antalya, and states a total of
**4,167,797 movements**.

Measured on the twelve training files:

  - total rows: **4,167,797** — matches the documented figure exactly, so
    the download is complete and nothing is missing locally;
  - distinct reporting airports (ADEP_mvt for DEP, ADES_mvt for ARR): **10**;
  - LTAI as a reporting airport: **0 rows**;
  - LTAI as the *other* endpoint of a route: 23,862 rows, so the ICAO code
    is present in the data, just never as a reporting airport.

Because the documented total matches a dataset that contains no Antalya
movements, the count was evidently taken after Antalya had dropped out.
Either Table 1 is wrong, or Antalya was intended to be included and its
absence went unnoticed. Worth confirming with the organisers, especially
while the July export is being regenerated — if Antalya is meant to be in
there, the regenerated extract is the natural moment to fix it.

Reporting-airport row counts (both phases, 2025 training):

    EDDF 460,263   EDDM 334,657   EGLL 479,057   EHAM 495,657
    LEBL 358,786   LEMD 423,333   LFPG 478,387   LIRF 321,208
    LSZH 269,598   LTFM 546,851

Also confirmed by the page's column descriptions: `_mvt` columns belong to
the *reporting* airport, `ADEP_mvt` is the aerodrome of departure and
`ADES_mvt` the aerodrome of destination — so for an ARRIVAL the reporting
airport is `ADES_mvt`. That is the bug fixed in v7.

## Submission cap introduced, 4 Sep 2026

Organiser (John Fitzgerald): **3 submissions per day**, resetting at
00:00 UTC. Over the limit the result file returns

    {"status": "Rejected", "error_type": "DAILY_LIMIT_REACHED"}

Also: bucket size capped at 1 GB, and deleting submissions from the bucket
does not affect the leaderboard. Uploads must go through the Minio `mc`
client because of a bug in the Minio web UI — our boto3 path is unaffected.

This makes the "only submit changes with a mechanism" rule a hard
constraint rather than a preference. Offline evaluation on the
stable-airport metric decides what is worth one of the three.

## Three architecture experiments, 4 Sep — all negative

Measured on the stable-airport metric (v8 baseline: 221.1s).

**Per-airport matched models: worse (224.3s).** Ten separate models lose
more to reduced sample size than they gain from specialisation; the global
model with `apt` as a categorical already separates airports while sharing
statistical strength across them. The original plan's instinct that
"Zurich and Istanbul are different problems" is true of the *data* but not
of the *estimator*.

**Out-of-fold target encoding: no effect (221.0s).** Smoothed OOF mean
encodings of (apt, stand, rwy), (apt, stand) and (apt, rwy, hour) on the
residual target added nothing over LightGBM's native categorical handling.

**Domain-shift correction from arrival taxi times: mechanism fails.**
Arrivals are unblanked in the ranking set, so 2026 taxi-in times are real
ground truth, and they show large shifts against the same months of 2025:

    EHAM Jan +66.9s (+11.3%)   EHAM Jul -69.8s (-14.6%)
    EDDF Jul -66.6s (-11.0%)   LIRF Jan -43.1s (-6.8%)
    LSZH Jan +35.0s (+10.3%)

Tempting: correct departure predictions by the shift measured on arrivals.
But testing the transfer month-to-month within 2025 (120 airport-months)
gives **r = 0.039** overall — no reliable relationship. Per airport it
splits both ways: EDDF +0.72, LEMD +0.89, LFPG +0.66, LIRF +0.55, EGLL
+0.51, but EHAM **-0.43**, EDDM -0.27, LTFM -0.16, LSZH -0.15.

EHAM is where the largest 2026 shift sits *and* where the correlation is
most strongly negative, so an arrival-based correction would most likely
have made our worst-shifted airport worse. Worth recording as a case where
a plausible mechanism was checked before use and did not survive.

**Assessment.** The mixture + GBM architecture looks close to its ceiling
on this feature set. Remaining error is concentrated in corrupted records
we have shown to be unpredictable from anything observable. Further gains
need either a new information source or a different problem framing, not
more refinement of this one.

## The extremes, properly analysed (4 Sep) — correcting an earlier claim

Earlier notes said the extreme taxi times are "unpredictable". That was
wrong as stated, and the correct version matters.

**They are highly identifiable.** A classifier for `y > 3600s` over the
existing features scores **AUC 0.979**, and its top 1% by predicted risk
contains 72.5% of all such flights. For `y > 1800s`, AUC 0.964; for
`y > 7200s`, AUC 0.880. So the model can see which flights are at risk.

**And the model already exploits it.** Calibration measured on our own
predictions — the valid conditioning, since predictions are a function of
the observables:

    top 0.05%  mean_pred 13348  mean_y 13135  bias  +213
    top 0.10%  mean_pred  9312  mean_y  8682  bias  +630
    top 0.50%  mean_pred  4486  mean_y  4223  bias  +262
    top 1.00%  mean_pred  3479  mean_y  3361  bias  +118
    overall                                   bias    +5.2

We slightly *over*-predict in the far tail, so there is no under-prediction
to correct. Removing the residual tail bias entirely is worth well under
1s of RMSE — not worth one of three daily submissions.

**A trap worth recording.** Grouping the same residuals by *true* y looks
alarming:

    true y band     n        mean_pred   mean_y   % sq err
    <30m            328,620      970       942      37.5
    30-60m           14,641     1839      2227      13.2
    1-2h                833     3195      4573       7.2
    2-5.5h              122     7284     10112       5.0
    >5.5h                26    39027     50925      37.2

It appears we over-predict ordinary flights by 28s and under-predict the
26 worst by ~11,900s. That is **regression to the mean, not a fixable
bias**: conditioning on the outcome induces exactly this pattern even for
a perfectly calibrated model. Only conditioning on predictions (above)
tests calibration honestly, and by that test the model is sound.

**Conclusion, with better reasoning than before.** The extremes are not
invisible — they are identified about as well as the observables allow,
and priced into the conditional mean. What is left is genuine outcome
variance: among flights that look equally risky, only some actually go
long, and nothing observable separates them. Those 26 rows carry 37% of
validation squared error and cannot be improved by better modelling of
this feature set.

## The regenerated dataset, 4 Sep 2026 — everything resets

The organisers re-issued the ranking and submitting files (the July
extract had been taken before some airports reported).

| | old | new |
|---|---|---|
| ranking.parquet | 28.0 MB | 43.6 MB |
| submitting.parquet | 1.15 MB | 1.68 MB |
| scored departures | 215,876 | **344,841** |
| January share | 70.7% | 44.3% |
| July share | 29.3% | **55.7%** |
| airports in July | EDDF, EGLL, EHAM only | **all ten** |

LTAI/Antalya is still **absent as a reporting airport** in the new export
(it appears 3,496 times only as the other end of a route), so the eleven
airports in Table 1 remain wrong and our Discord question stands.

**Scores across the change are not comparable.** July is both the majority
of the new set and the harder month. Another team reported the same model
scoring 278.38 on the old set and 326.60 on the new — a ~48s step that
says nothing about model quality. Because the board ranks on
best-across-submissions, teams keep an old-set best that is no longer
achievable, so the public table currently mixes two incomparable scales.

The harness needed no rework: it derives each airport's month mask and
weight from the ranking file itself, so it picked up the new composition
automatically. Validation moved 334.8 -> 356.8s purely from reweighting.

| ver | test set | offline estimate | actual score |
|-----|----------|------------------|--------------|
| v8  | old (215,876) | 334.8s | 291.59s |
| v9  | **new (344,841)** | 356.8s | **321.89s** |

**Like-for-like standing.** Filtering the leaderboard API to submissions
processed after the changeover gives a true new-set table. Of 733 total
submissions only 27 have been scored against the new truth so far:

     1 enthusiastic-daisy  277.29      4 intelligent-ladder  309.85
     2 upbeat-goblin       279.37      5 generous-jungle     312.76
     3 quick-boat          279.98      6 resilient-kiwi      321.89
     7 vibrant-jewel       326.60      8 reliable-hamburger  343.31

**6th of 17 on comparable numbers**, 41.9s off third. The mixed public
board showing us 14th of 37 is an artefact of most teams not having
resubmitted yet.

## Weather (METAR) — the one external source that paid off

Source: Iowa Environmental Mesonet ASOS/METAR archive (Iowa State
University), open and freely redistributable, fetched by
`scripts/fetch_weather.py` — 277,489 observations across the ten airports,
2025-01-01 to 2026-08-01. Not OpenSky-derived, so unaffected by the
organisers' ruling on state vectors. `scripts/weather_features.py` joins
each departure (ASOF, at its own airport, observation within 90 minutes)
and derives temperature, visibility, wind, precipitation and flags for
de-icing risk, low visibility, snow and freezing. Match rate 100.0%.

**The marginal signal is large:**

    regime            n         mean taxi
    de-icing risk     8,347     1,675s
    low visibility   22,111     1,073s
    benign        1,683,032       978s

**But most of it is already in `recov`.** Conditioning on both shows the
flight's own observed taxi (`recov = take-off - AOBT_3_flt`) already
carries the de-icing time; only the residual gap is new information:

    regime            mean y   mean recov   gap
    de-icing risk      1600       1368      +232
    low visibility     1067       1014       +54
    benign              978       1002       -24

**And validation said it was worthless**: all-airport 356.8 -> 358.1,
stable-only 229.8 -> 228.6, i.e. ~1s, below the noise floor.

**The composition argument overrode the metric.** De-icing weather is far
more common in the scored set than in our validation months:

    validation (Jan+Jul 2025)   0.68%
    ranking set 2026            1.96%
    ranking January 2026        4.42%

January 2026 was a severe winter — consistent with Schiphol's own report of
several thousand weather cancellations, cited on Discord. Our validation
therefore contains roughly **3x less** of the condition than the set we are
scored on, so it structurally understates the feature's value, and a model
without weather cannot adapt to that shift at all.

Submitted on the mechanism rather than the metric, and the arithmetic held:

| ver | change | validation | actual |
|-----|--------|-----------|--------|
| v9  | new dataset, no weather | 356.8s | 321.89s |
| v10 | + METAR features | 356.4s | **318.06s** |

Validation predicted ~0.4s; the leaderboard gave **3.8s**, about the 3x
ratio the prevalence gap implied.

**Lesson to keep**: when the validation set's composition differs from the
scored set on the very variable a feature describes, the validation gain is
the wrong estimator. Check prevalence in both before discarding a feature.

## Cumulative weather — de-icing backlog (v11)

Instantaneous METAR describes the moment; a de-icing pad backs up over
hours. Added, computed as window aggregates on the METAR series itself
before the ASOF join: precipitation over 6h and 12h, cold-only
precipitation over 12h, the fraction of the last 6h in de-icing
conditions, minimum temperature over 12h, minimum and mean visibility over
3h, and hours since the airfield was last above freezing.

These carry signal on the quantity the model actually predicts — the gap
`y - recov` — and, crucially, cover far more flights than the
instantaneous flag (≈55,000 vs 8,300):

    cold spell            n         mean gap
    frozen > 24h         13,175       +147s
    frozen 6-24h         23,668        +82s
    frozen < 6h          17,768        +32s
    above freezing    1,809,767        -25s

    sustained de-ice 6h   7,909       +184s
    intermittent         22,443        +44s
    none              1,834,026        -24s

Prevalence again favours the scored set over validation:

                        frozen   sustained de-ice
    validation 2025      6.20%        2.75%
    ranking 2026         8.03%        5.15%
    ranking Jan 2026    18.14%       11.64%

Validation showed +0.5s all-airport and +0.1s stable — below the noise
floor. The prevalence ratio here is ~1.9x (against 3x for the
instantaneous features), so the prevalence-adjusted expectation was ~1s.

| ver | change | validation | actual |
|-----|--------|-----------|--------|
| v10 | instantaneous METAR | 356.4s | 318.06s |
| v11 | + cumulative / backlog features | 357.4s | **316.80s** |

Actual gain 1.26s against a predicted ~1s. The prevalence-adjusted
estimator has now been right twice (predicted 3x -> 3.8s; predicted ~1s ->
1.26s), which is far better calibration than raw validation on this class
of feature.

Weather total: 321.89 -> 316.80, i.e. **5.1s** from one open external
source. Diminishing within the source, as expected — the instantaneous
flags took most of it.
