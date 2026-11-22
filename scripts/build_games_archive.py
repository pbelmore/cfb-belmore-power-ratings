#!/usr/bin/env python3
"""
build_games_archive.py -- one-time (or occasional) builder for
data/games_archive.json: every completed FBS-vs-FBS game from every season
in ratings_history.json, in the same row format as games.json. It feeds
rivalries.html, which pairs each game with the published pre-game snapshot
already in ratings_history.json -- no ratings are computed here.

games.json only ever holds the current season (it's overwritten at each
season rollover), so weekly_update.py keeps the archive's current season
in sync on every run (see archive_season_games()); this script is only
needed to (re)build past seasons.

"FBS" per season = the teams that season's ratings_history.json snapshots
cover, so a game against an FCS opponent (or a team still transitioning
up) is left out -- it has no rating on either side to show.

Costs one CFBD call per season. Only writes data/games_archive.json.

Usage:
  CFBD_API_KEY=xxxx python scripts/build_games_archive.py                 # every season on record
  CFBD_API_KEY=xxxx python scripts/build_games_archive.py --start-year 2025 --end-year 2025
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from weekly_update import archive_season_games, fetch_games, load_json, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start-year", type=int, default=None, help="default: first season in ratings_history.json")
    parser.add_argument("--end-year", type=int, default=None, help="default: last season in ratings_history.json")
    parser.add_argument("--out-dir", default="data")
    args = parser.parse_args()

    api_key = os.environ.get("CFBD_API_KEY")
    if not api_key:
        sys.exit("CFBD_API_KEY environment variable is not set.")

    out_dir = Path(args.out_dir)
    history = load_json(out_dir / "ratings_history.json", [])
    if not history:
        sys.exit(f"No {out_dir}/ratings_history.json to take each season's FBS teams from.")
    fbs_by_season = {}
    for r in history:
        fbs_by_season.setdefault(r["season"], set()).add(r["team"])

    start = args.start_year or min(fbs_by_season)
    end = args.end_year or max(fbs_by_season)
    archive_path = out_dir / "games_archive.json"
    archive = load_json(archive_path, [])

    for year in range(start, end + 1):
        if year not in fbs_by_season:
            print(f"{year}: not in ratings_history.json -- skipped")
            continue
        game_rows = fetch_games(year, api_key)
        archive = archive_season_games(archive, year, game_rows, fbs_by_season[year])
        kept = sum(1 for r in archive if r["season"] == year)
        print(f"{year}: {kept} FBS-vs-FBS games (of {len(game_rows)} completed)")

    write_json(archive_path, archive)
    print(f"\nWrote {archive_path} ({len(archive)} games)")


if __name__ == "__main__":
    main()
