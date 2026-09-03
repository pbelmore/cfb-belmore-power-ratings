#!/usr/bin/env python3
"""
weekly_update.py -- pulls this season's FBS teams and completed games from
the CFBD API, runs them through ratings_core, and writes:

  data/teams.json            current-season roster (school, conference)
  data/games.json             one row per completed game
  data/ratings_history.json   long-format, one row per team per run (appended)

Usage:
  CFBD_API_KEY=xxxx python scripts/weekly_update.py --year 2026
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ratings_core import Game, build_snapshot_rows, compute_ratings

API_BASE = "https://api.collegefootballdata.com"

# Game.location codes <-> the human-readable words stored in games.json
CODE_TO_WORD = {"": "home", "@": "away", "N": "neutral"}
WORD_TO_CODE = {v: k for k, v in CODE_TO_WORD.items()}


def cfbd_get(path, api_key, **params):
    resp = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {api_key}"},
        params={k: v for k, v in params.items() if v is not None},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_teams(year, api_key):
    raw = cfbd_get("/teams/fbs", api_key, year=year)
    return [{"school": t["school"], "conference": t.get("conference")} for t in raw]


def fetch_games(year, api_key, through_week=None):
    raw = cfbd_get("/games", api_key, year=year, seasonType="both", classification="fbs")
    games = []
    for g in raw:
        home_pts, away_pts = g.get("homePoints"), g.get("awayPoints")
        if not g.get("completed") or home_pts is None or away_pts is None:
            continue
        if through_week is not None and (g.get("week") or 0) > through_week:
            continue

        home, away = g["homeTeam"], g["awayTeam"]
        neutral = bool(g.get("neutralSite"))
        if home_pts > away_pts:
            winner, winner_pts, loser, loser_pts = home, home_pts, away, away_pts
            location = "neutral" if neutral else "home"
        else:
            winner, winner_pts, loser, loser_pts = away, away_pts, home, home_pts
            location = "neutral" if neutral else "away"

        games.append({
            "season": g.get("season", year),
            "week": g.get("week"),
            "date": g.get("startDate"),
            "winner": winner,
            "winner_pts": winner_pts,
            "loser": loser,
            "loser_pts": loser_pts,
            "location": location,
            "notes": g.get("notes") or "",
        })
    return games


def to_rating_games(game_rows):
    return [
        Game(
            winner=row["winner"],
            winner_pts=row["winner_pts"],
            loser=row["loser"],
            loser_pts=row["loser_pts"],
            location=WORD_TO_CODE[row["location"]],
        )
        for row in game_rows
    ]


def load_json(path, default):
    if path.exists():
        with path.open() as f:
            return json.load(f)
    return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument(
        "--week", type=int, default=None,
        help="Only include games through this regular-season week (default: all "
             "completed games so far). Not recommended once postseason games "
             "start, since postseason week numbers restart at 1.",
    )
    parser.add_argument(
        "--as-of", default=None,
        help="Label for this run in ratings_history.json (default: today's UTC date)",
    )
    parser.add_argument("--out-dir", default="data")
    args = parser.parse_args()

    api_key = os.environ.get("CFBD_API_KEY")
    if not api_key:
        sys.exit("CFBD_API_KEY environment variable is not set.")

    as_of = args.as_of or datetime.now(timezone.utc).date().isoformat()
    out_dir = Path(args.out_dir)

    print(f"Fetching {args.year} FBS teams...")
    teams = fetch_teams(args.year, api_key)
    print(f"  {len(teams)} teams")

    print(f"Fetching {args.year} games...")
    game_rows = fetch_games(args.year, api_key, through_week=args.week)
    print(f"  {len(game_rows)} completed games")

    write_json(out_dir / "teams.json", teams)
    write_json(out_dir / "games.json", game_rows)

    print("Computing ratings...")
    results = compute_ratings(teams, to_rating_games(game_rows))
    new_rows = build_snapshot_rows(results, teams, season=args.year, as_of=as_of)

    history_path = out_dir / "ratings_history.json"
    history = load_json(history_path, [])
    # Replace any existing rows for this (season, as_of) so re-running the
    # same day's snapshot updates it in place instead of duplicating it.
    history = [r for r in history if not (r["season"] == args.year and r["as_of"] == as_of)]
    history.extend(new_rows)
    write_json(history_path, history)

    top = sorted(results.items(), key=lambda kv: kv[1]["rating"], reverse=True)[:5]
    print(f"\nTop 5 as of {as_of}:")
    for i, (team, r) in enumerate(top, start=1):
        print(f"  {i}. {team} ({r['wins']}-{r['losses']}) -- {r['rating']:.4f}")

    print(f"\nWrote {out_dir}/teams.json, {out_dir}/games.json, {out_dir}/ratings_history.json")


if __name__ == "__main__":
    main()
