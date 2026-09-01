# Plan and open questions

Challenge window: 1 Sep – 11 Oct 2026, 23:59:59 CET. Best RMSE across all
submissions counts.

## Week 1
- [ ] Get MinIO access key, download all data (`fetch_data.py`)
- [ ] Run `inspect_ranking.py` — resolve the AOBT_3_flt question
- [ ] Ask on Discord (PRC Data Challenge 2026) whether using surviving
      flight-list off-block times in ranking.parquet is within the rules.
      Organisers warn against exploiting the ranking process; do not rely
      on it before an explicit answer.
- [ ] Baseline submission `resilient-kiwi_v1.parquet` to lock a score

## Then
- [ ] Empirical unimpeded taxi time per airport × stand × runway
      (PRC "additional taxi-out time" methodology)
- [ ] Departure queue feature: count of aircraft off-block and not yet
      airborne at the airport in the preceding window
- [ ] Per-airport gradient-boosted models (LightGBM), validated on
      Jan + Jul 2025 only
- [ ] Public GitHub repo under GPLv3 + reproduction docs before any
      prize claim

## Guardrails
- Entry is via the Swedish AB (White Pearl Technology Group AB). Do not
  introduce any other national affiliation in repo or paper without
  checking eligibility rules first.
- No WPTG platform IP in this repo — everything here goes public.
- Every submission goes through `validate_submission.py` first.
