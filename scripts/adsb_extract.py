"""Extract ground segments at the ten airports from one day of adsb.lol traces.

adsb.lol publishes one archive per day (https://github.com/adsblol/globe_history_2026,
ODbL / CC0). Inside, traces/<xx>/trace_full_<hex>.json are readsb trace
JSONs (gzip-compressed regardless of extension). Each trace entry is
[dt, lat, lon, alt_or_"ground", gs, track, flags, vrate, details, ...].

For every aircraft that was on the ground inside one of our airport boxes we
emit one row per ground segment with the timings a departure model needs:
first ground observation, first observation moving, last ground point, first
airborne point, and the callsign seen during the segment.

    uv run scripts/adsb_extract.py data/adsb/2025-07-15 data/adsb/segments_2025-07-15.parquet
"""
from __future__ import annotations

import gzip
import json
import os
import sys
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

# Reference points; boxes are generous so remote stands and long runways stay in.
AIRPORTS = {
    "EDDF": (50.0333, 8.5706), "EDDM": (48.3538, 11.7861), "EGLL": (51.4700, -0.4543),
    "EHAM": (52.3086, 4.7639), "LEBL": (41.2971, 2.0785), "LEMD": (40.4719, -3.5626),
    "LFPG": (49.0097, 2.5479), "LIRF": (41.8003, 12.2389), "LSZH": (47.4647, 8.5492),
    "LTFM": (41.2753, 28.7519),
}
DLAT, DLON = 0.06, 0.09
MOVING_KT = 3.0          # ground speed that counts as "moving"
MAX_GAP_S = 1800         # a gap longer than this splits a ground run
AIR_WITHIN_S = 1200      # first airborne point must follow the run within this


def which_apt(lat: float, lon: float) -> str | None:
    for apt, (la, lo) in AIRPORTS.items():
        if abs(lat - la) <= DLAT and abs(lon - lo) <= DLON:
            return apt
    return None


def load(path: str) -> dict | None:
    with open(path, "rb") as fh:
        head = fh.read(2)
        fh.seek(0)
        raw = gzip.open(fh).read() if head == b"\x1f\x8b" else fh.read()
    try:
        return json.loads(raw)
    except Exception:
        return None


def process(path: str) -> list[dict]:
    d = load(path)
    if not d or "trace" not in d:
        return []
    t0 = d.get("timestamp", 0)
    hexid = d.get("icao")
    pts = []
    for e in d["trace"]:
        if len(e) < 9 or e[1] is None or e[2] is None:
            continue
        pts.append((t0 + e[0], e[1], e[2], e[3], e[4], e[6] or 0, e[8]))
    out = []
    run = []          # current ground run at one airport

    def flush(run, next_pt):
        if len(run) < 2:
            return
        apt = run[0][0]
        first = run[0][1]
        moving = next((p for _, p in run if (p[4] or 0) >= MOVING_KT), None)
        last = run[-1][1]
        air = None
        if next_pt is not None and next_pt[3] != "ground" and next_pt[0] - last[0] <= AIR_WITHIN_S:
            air = next_pt
        cs = Counter()
        for _, p in run:
            det = p[6]
            if isinstance(det, dict) and det.get("flight"):
                cs[det["flight"].strip()] += 1
        if air is None:
            for p in pts:  # callsign may only appear once airborne
                if p[0] > last[0] and isinstance(p[6], dict) and p[6].get("flight"):
                    cs[p[6]["flight"].strip()] += 1
                    break
        out.append(dict(
            hex=hexid, apt=apt,
            callsign=cs.most_common(1)[0][0] if cs else None,
            t_first_ground=first[0], t_first_moving=moving[0] if moving else None,
            t_last_ground=last[0], t_first_air=air[0] if air else None,
            n_ground=len(run), lat0=first[1], lon0=first[2],
            gs_first=first[4], stale_first=bool(first[5] & 1),
        ))

    prev_t = None
    for p in pts:
        on_ground = p[3] == "ground"
        apt = which_apt(p[1], p[2]) if on_ground else None
        if apt and run and run[-1][0] == apt and (p[0] - prev_t) <= MAX_GAP_S:
            run.append((apt, p))
        else:
            if run:
                flush(run, p)
            run = [(apt, p)] if apt else []
        prev_t = p[0]
    if run:
        flush(run, None)
    return out


def main() -> None:
    root, dest = Path(sys.argv[1]), Path(sys.argv[2])
    files = [str(p) for p in root.rglob("trace_full_*.json")]
    print(f"{len(files):,} trace files", flush=True)
    rows = []
    with Pool(max(1, os.cpu_count() - 1)) as pool:
        for i, r in enumerate(pool.imap_unordered(process, files, chunksize=64)):
            rows.extend(r)
            if i % 50000 == 0:
                print(f"  {i:,} files, {len(rows):,} segments", flush=True)
    df = pd.DataFrame(rows)
    df.to_parquet(dest, index=False)
    print(f"wrote {len(df):,} segments -> {dest}")
    print(df.groupby("apt").size().to_string())


if __name__ == "__main__":
    main()
