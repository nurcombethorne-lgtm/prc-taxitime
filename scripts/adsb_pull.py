"""Stream adsb.lol daily archives: download, extract ground segments at the
ten airports (adsb_extract.py), delete the archive. One small parquet per
day lands in data/adsb/segments/. Idempotent: days already done are skipped.

    nohup uv run scripts/adsb_pull.py > data/adsb/pull.log 2>&1 &
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "adsb" / "segments"
TMP = ROOT / "data" / "adsb" / "tmp"
EXTRACT = ROOT / "scripts" / "adsb_extract.py"


def days_from_ranking(path: str) -> list[date]:
    """Every departure date in a ranking file, so a re-issued or hidden-stage
    dataset can be processed without editing this script:

        uv run scripts/adsb_pull.py --ranking data/ranking.parquet
    """
    import duckdb
    rows = duckdb.connect().sql(
        f"SELECT DISTINCT MVT_TIME_UTC_mvt::DATE d FROM '{path}' WHERE PHASE_mvt='DEP' ORDER BY 1"
    ).fetchall()
    return [r[0] for r in rows]


def days() -> list[date]:
    if "--ranking" in sys.argv:
        extra = days_from_ranking(sys.argv[sys.argv.index("--ranking") + 1])
        return [d for d in extra] + _default_days()
    return _default_days()


def _default_days() -> list[date]:
    scored = [date(2026, 1, 1) + timedelta(d) for d in range(32)]          # Jan 1 .. Feb 1
    scored += [date(2026, 7, 1) + timedelta(d) for d in range(32)]         # Jul 1 .. Aug 1
    # Training days with ground truth: the same weekdays spread across the
    # two validation months plus a few from other months, so the model sees
    # the feature in both seasons and in fit months.
    train = [date(2025, 1, d) for d in (6, 9, 12, 15, 18, 21, 24, 27)]
    train += [date(2025, 7, d) for d in (3, 6, 9, 12, 18, 21, 24, 27)]
    # Fit-month days (the validation harness fits on Feb-Jun + Aug-Dec), so
    # the feature is learnt from fit rows and not only from the two scored
    # months: two days per month, a weekday and a weekend.
    train += [date(2025, m, d) for m in (2, 3, 4, 5, 6, 8, 9, 10, 11, 12) for d in (10, 21)]
    # Second tranche (23 Sep): three more days per fit month and four more per
    # scored month of 2025, to sharpen the per-airport trace behaviour.
    train += [date(2025, m, d) for m in (2, 3, 4, 5, 6, 8, 9, 10, 11, 12) for d in (4, 15, 27)]
    train += [date(2025, 1, d) for d in (3, 10, 17, 30)] + [date(2025, 7, d) for d in (1, 8, 22, 29)]
    # Third tranche (27 Sep): every remaining day of January and July 2025,
    # the two scored months, after other teams reported that a small sample
    # of days was what had made the traces look unhelpful.
    train += [date(2025, 1, 1) + timedelta(d) for d in range(31)]
    train += [date(2025, 7, 1) + timedelta(d) for d in range(31)]
    # Fourth tranche (29 Sep): every remaining day of 2025. The scored set is
    # 60-98% traced at EGLL and LEMD, which 2025 covered only partly, and a
    # leave-one-airport-out test put the value of airport-specific traced
    # training rows at ~18 s per airport (experiment_transfer.py).
    train += [date(2025, 1, 1) + timedelta(d) for d in range(365)]
    seen, out = set(), []
    for d in train + scored:
        if d not in seen:
            seen.add(d); out.append(d)
    return out


def assets(day: date) -> list[str]:
    repo = f"globe_history_{day.year}"
    # Some days are published only under a replica tag (see
    # PREFERRED_RELEASES.txt in the adsblol repos); try them in order.
    last = None
    for suffix in ("prod-0", "staging-0", "prod-0tmp", "staging-0tmp"):
        tag = f"v{day:%Y.%m.%d}-planes-readsb-{suffix}"
        url = f"https://api.github.com/repos/adsblol/{repo}/releases/tags/{tag}"
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                rel = json.load(r)
        except urllib.error.HTTPError as e:      # this tag does not exist: try the next replica
            last = e
            continue
        except Exception as e:                   # network down: wait it out rather than skip the day
            for _ in range(120):
                time.sleep(60)
                try:
                    urllib.request.urlopen("https://api.github.com", timeout=30)
                    break
                except urllib.error.HTTPError:
                    break
                except Exception:
                    continue
            return assets(day)
        urls = [a["browser_download_url"] for a in rel["assets"] if ".tar" in a["name"]]
        if urls:
            return urls
    raise last or RuntimeError("no release")


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    def done(d: date) -> bool:
        f = OUT / f"{d}.parquet"
        if not f.exists():
            return False
        import pyarrow.parquet as pq
        return "actype" in pq.read_schema(f).names      # schema 3: movement + aircraft type/registration

    # Scored months of 2025 first, then the rest of 2025, then the 2026 days.
    order = sorted(days(), key=lambda d: (d.year == 2026, d.month not in (1, 7), d))
    todo = [d for d in order if not done(d)]
    print(f"{len(todo)} days to do", flush=True)
    for day in todo:
        t0 = time.time()
        work = TMP / str(day)
        shutil.rmtree(TMP, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            urls = sorted(assets(day))
            parts = []
            for u in urls:
                p = TMP / u.rsplit("/", 1)[1]
                # -C - resumes a partial file after a connection reset instead of
                # restarting the 2 GB part from zero (8 Oct: resets every few
                # minutes made a 6-minute day take an hour).
                for attempt in range(12):
                    r = subprocess.run(["curl", "-sSL", "-C", "-", "--retry", "3", "--retry-all-errors",
                                        "--retry-delay", "10", "-o", str(p), u])
                    if r.returncode == 0:
                        break
                    time.sleep(20)
                else:
                    raise RuntimeError(f"download failed after 12 attempts: {u}")
                parts.append(p)
            with open(TMP / "all.tar", "wb") as fh:
                for p in parts:
                    with open(p, "rb") as src:
                        shutil.copyfileobj(src, fh, 1 << 24)
                    p.unlink()
            run(["tar", "-xf", str(TMP / "all.tar"), "-C", str(work)])
            (TMP / "all.tar").unlink()
            run(["uv", "run", str(EXTRACT), str(work), str(OUT / f"{day}.parquet")])
            print(f"OK {day}  {time.time() - t0:.0f}s", flush=True)
        except Exception as e:  # keep going; a failed day is retried on the next run
            print(f"FAIL {day}: {e}", flush=True)
        finally:
            shutil.rmtree(TMP, ignore_errors=True)
    print("PULL_DONE", flush=True)


if __name__ == "__main__":
    main()
