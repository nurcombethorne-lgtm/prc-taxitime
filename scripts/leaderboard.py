"""Fetch the challenge leaderboard from the API.

The public Observable notebook broke when Observable shipped their new
notebook framework on 1 Sep 2026 (organiser statement on Discord), so the
standings are read straight from the API it was calling. Endpoint and
competition id were taken from the notebook source.

The API returns one row per *submission*, cursor-paginated, so this
reduces to the best score per team.

    uv run scripts/leaderboard.py
"""

from __future__ import annotations

import json
import urllib.request
from collections import Counter

BASE = ("https://datacomp.opensky-network.org/api/competitions/"
        "bb3693e1-26bc-4a9e-8619-4fe78b4eab0c/leaderboard?limit=200")
US = "resilient-kiwi"


def fetch() -> list[dict]:
    items: list[dict] = []
    cursor = None
    for _ in range(50):  # guard against a runaway cursor loop
        url = BASE + (f"&cursor={cursor}" if cursor else "")
        with urllib.request.urlopen(url, timeout=30) as r:
            page = json.load(r)
        items += page["items"]
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return items


def main() -> None:
    items = fetch()
    counts = Counter(i["teamName"] for i in items)
    best: dict[str, dict] = {}
    for it in items:
        if it.get("score") is None:
            continue
        cur = best.get(it["teamName"])
        if cur is None or it["score"] < cur["score"]:
            best[it["teamName"]] = it

    rows = sorted(best.values(), key=lambda r: r["score"])
    print(f"{len(items)} submissions, {len(rows)} teams\n")
    print(f"{'#':>3} {'team':24s} {'best':>9s} {'subs':>5s}")
    for i, r in enumerate(rows, 1):
        team = r["teamName"]
        mark = "  <== us" if team == US else ""
        print(f"{i:3d} {team:24s} {r['score']:9.2f} {counts[team]:5d}{mark}")

    if US in best:
        rank = next(i for i, r in enumerate(rows, 1) if r["teamName"] == US)
        podium = rows[2]["score"] if len(rows) > 2 else None
        print(f"\n{US}: rank {rank}/{len(rows)} at {best[US]['score']:.2f}")
        if podium is not None and best[US]["score"] > podium:
            print(f"  {best[US]['score'] - podium:.2f}s from third place")


if __name__ == "__main__":
    main()
