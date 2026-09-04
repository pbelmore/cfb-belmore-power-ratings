#!/usr/bin/env python3
"""
backfill_history.py -- reconstructs week-by-week ratings_history rows for
past seasons from CFBD, at a cost of 2 API calls per season (teams once,
games once) plus 2 more for any season with a prior year on record (to
recompute that prior year's raw final ratings fresh, for the preseason
blend -- see weekly_update.prior_season_raw_ratings). Every week's
cumulative snapshot is then built locally by filtering that one game list
-- no per-week API calls.

Regular-season weeks are snapshotted 1..max, cumulatively. Postseason games
(bowls/CFP) are folded into a single final snapshot per season instead of
their own weekly ones, since postseason week numbers restart at 1 and would
otherwise collide with regular-season week numbers.

Only writes data/ratings_history.json -- unlike weekly_update.py, this does
NOT touch teams.json/games.json, since those represent current-season state,
not a historical archive.

Usage:
  CFBD_API_KEY=xxxx python scripts/backfill_history.py --start-year 2010 --end-year 2025
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from weekly_update import (
    WORD_TO_CODE,
    blend_raw_ratings,
    build_public_rows,
    cfbd_get,
    fetch_teams,
    load_json,
    normalize_values,
    prior_season_raw_ratings,
    write_json,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ratings_core import Game, compute_ratings


def fetch_completed_game_rows(year, api_key):
    raw = cfbd_get("/games", api_key, year=year, seasonType="both", classification="fbs")
    rows = []
    for g in raw:
        home_pts, away_pts = g.get("homePoints"), g.get("awayPoints")
        if not g.get("completed") or home_pts is None or away_pts is None:
            continue
        home, away = g["homeTeam"], g["awayTeam"]
        neutral = bool(g.get("neutralSite"))
        if home_pts > away_pts:
            winner, winner_pts, loser, loser_pts = home, home_pts, away, away_pts
            location = "neutral" if neutral else "home"
        else:
            winner, winner_pts, loser, loser_pts = away, away_pts, home, home_pts
            location = "neutral" if neutral else "away"
        rows.append({
            "season_type": g.get("seasonType"),
            "week": g.get("week"),
            "date": g.get("startDate"),
            "winner": winner,
            "winner_pts": winner_pts,
            "loser": loser,
            "loser_pts": loser_pts,
            "location": location,
        })
    rows.sort(key=lambda r: r["date"] or "")
    return rows


def build_snapshots(game_rows):
    """Yields (as_of_label, cumulative_game_rows, week, stage) -- one per
    regular-season week boundary, plus one final snapshot folding in
    postseason (if any). The final snapshot's week is just "well past 6",
    since postseason week numbers restart at 1 and preseason blending only
    ever needs to know it's no longer in the first 6 weeks. `stage` drives
    the site's "End of Regular Season" / "End of Bowl Season" labels."""
    regular = [r for r in game_rows if r["season_type"] == "regular" and r["week"] is not None]
    postseason = [r for r in game_rows if r["season_type"] != "regular"]

    weeks = sorted({r["week"] for r in regular})
    for wk in weeks:
        cumulative = [r for r in regular if r["week"] <= wk]
        as_of = max(r["date"] for r in cumulative if r["date"])[:10]
        yield as_of, cumulative, wk, "regular"

    if postseason:
        final = regular + postseason
        as_of = max(r["date"] for r in final if r["date"])[:10]
        yield as_of, final, 99, "postseason"


def to_rating_games(rows):
    return [
        Game(r["winner"], r["winner_pts"], r["loser"], r["loser_pts"], WORD_TO_CODE[r["location"]])
        for r in rows
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start-year", type=int, required=True)
    parser.add_argument("--end-year", type=int, required=True)
    parser.add_argument("--out-dir", default="data")
    args = parser.parse_args()

    api_key = os.environ.get("CFBD_API_KEY")
    if not api_key:
        sys.exit("CFBD_API_KEY environment variable is not set.")

    out_dir = Path(args.out_dir)
    history_path = out_dir / "ratings_history.json"
    history = load_json(history_path, [])

    call_count = 0
    for year in range(args.start_year, args.end_year + 1):
        print(f"=== {year} ===")
        teams = fetch_teams(year, api_key)
        call_count += 1
        game_rows = fetch_completed_game_rows(year, api_key)
        call_count += 1
        print(f"  {len(teams)} teams, {len(game_rows)} completed games")

        prior_raw = prior_season_raw_ratings(history, year, api_key)
        if prior_raw is not None:
            call_count += 2

        season_new_rows = []
        for as_of, cumulative, week, stage in build_snapshots(game_rows):
            results = compute_ratings(teams, to_rating_games(cumulative))
            blended_raw = blend_raw_ratings(results, prior_raw, week)
            scores = normalize_values(blended_raw)
            season_new_rows.extend(build_public_rows(results, scores, teams, season=year, as_of=as_of, stage=stage))

        as_ofs_this_run = {(year, r["as_of"]) for r in season_new_rows}
        history = [r for r in history if (r["season"], r["as_of"]) not in as_ofs_this_run]
        history.extend(season_new_rows)
        # Sort deterministically -- compute_ratings() iterates a set() internally,
        # whose order varies per process (Python string-hash randomization), so
        # without this every run would rewrite the whole file's line order.
        history.sort(key=lambda r: (r["season"], r["as_of"], r["team"]))
        write_json(history_path, history)
        print(f"  wrote {len(season_new_rows)} rows across {len(as_ofs_this_run)} snapshots")

    print(f"\nTotal API calls: {call_count}")


if __name__ == "__main__":
    main()
