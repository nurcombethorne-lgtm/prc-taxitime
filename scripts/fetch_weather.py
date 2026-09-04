"""Download METAR observations for the ten reporting airports.

Source: Iowa Environmental Mesonet (IEM) ASOS/METAR archive, Iowa State
University — https://mesonet.agron.iastate.edu/request/download.phtml

This is an open, freely redistributable archive of routine aerodrome
weather reports, which satisfies the challenge's requirement that external
datasets be open and documented. Nothing here is derived from OpenSky
state vectors, which the organisers ruled inadmissible on 3 Sep 2026.

Why weather: de-icing, low-visibility procedures and runway
reconfiguration in strong winds all lengthen taxi-out, and none of them
are visible in the movement or flight tables.

Coverage fetched is 2025-01-01 to 2026-08-01, spanning the twelve training
months and both ranking months. One request per airport, cached to
data/weather/<ICAO>.csv, so re-runs are free.

    uv run scripts/fetch_weather.py
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from s3util import DATA_DIR

AIRPORTS = ["EDDF", "EDDM", "EGLL", "EHAM", "LEBL",
            "LEMD", "LFPG", "LIRF", "LSZH", "LTFM"]
OUT = DATA_DIR / "weather"
BASE = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
FIELDS = ["tmpc", "dwpc", "vsby", "sknt", "drct", "p01i", "wxcodes"]
START = (2025, 1, 1)
END = (2026, 8, 1)


def url_for(station: str, y1, m1, d1, y2, m2, d2) -> str:
    parts = [f"station={station}"] + [f"data={f}" for f in FIELDS] + [
        f"year1={y1}", f"month1={m1}", f"day1={d1}",
        f"year2={y2}", f"month2={m2}", f"day2={d2}",
        "tz=UTC", "format=onlycomma", "latlon=no", "missing=M",
        "trace=T", "direct=no",
        "report_type=3", "report_type=4",   # routine + special reports
    ]
    return BASE + "?" + "&".join(parts)


# The service streams large responses with chunked encoding and regularly
# truncates a full multi-year pull, so fetch a year at a time via curl,
# which retries and validates the transfer.
CHUNKS = [(2025, 1, 1, 2026, 1, 1), (2026, 1, 1, 2026, 8, 1)]


def fetch(station: str) -> bytes:
    out = b""
    for j, (y1, m1, d1, y2, m2, d2) in enumerate(CHUNKS):
        if j:
            time.sleep(2)
        r = subprocess.run(
            ["curl", "-sS", "--fail", "--retry", "6", "--retry-delay", "5", "--retry-all-errors",
             "--max-time", "600", url_for(station, y1, m1, d1, y2, m2, d2)],
            capture_output=True)
        if r.returncode != 0:
            raise SystemExit(f"{station} {y1}: curl failed: {r.stderr.decode()[:200]}")
        body = r.stdout
        if j:                                  # drop the repeated header
            body = body.split(b"\n", 1)[1] if b"\n" in body else b""
        out += body
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for i, station in enumerate(AIRPORTS):
        dest = OUT / f"{station}.csv"
        if dest.exists() and dest.stat().st_size > 10_000:
            print(f"  skip {station} ({dest.stat().st_size:,} bytes cached)")
            continue
        if i:
            time.sleep(2)  # be polite to a free public service
        print(f"  get  {station} ...", end="", flush=True)
        body = fetch(station)
        dest.write_bytes(body)
        print(f" {len(body):,} bytes, {body.count(b'\n'):,} rows")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
