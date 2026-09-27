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
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "adsb" / "segments"
TMP = ROOT / "data" / "adsb" / "tmp"
EXTRACT = ROOT / "scripts" / "adsb_extract.py"


def days() -> list[date]:
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
        except Exception as e:
            last = e
            continue
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
                run(["curl", "-sSL", "--retry", "8", "--retry-all-errors", "--retry-delay", "15",
                     "-o", str(p), u])
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
