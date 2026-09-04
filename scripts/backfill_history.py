#!/usr/bin/env python3
"""
backfill_history.py -- reconstructs week-by-week ratings_history rows for
past seasons from CFBD, at a cost of 4 API calls per season (teams, games,
that season's committee rankings, and its real CFP/BCS field, each once)
plus 2 more for any season with a prior year on record (to recompute that
prior year's raw final ratings fresh, for the preseason blend -- see
weekly_update.prior_season_raw_ratings). Every week's cumulative snapshot
is then built locally by filtering that one game list -- no per-week API
calls.

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
    committee_ranks_for_week,
    fetch_cfp_participants,
    fetch_committee_rankings,
    fetch_teams,
    load_json,
    normalize_values,
    prior_season_raw_ratings,
    real_field_projection,
    real_playoff_field,
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


def _is_army_navy_only(games):
    return bool(games) and all({g["winner"], g["loser"]} == {"Army", "Navy"} for g in games)


def build_snapshots(game_rows):
    """Yields (as_of_label, cumulative_game_rows, week, stage) -- one per
    regular-season week boundary, plus one final snapshot folding in
    postseason (if any). The final snapshot's week is just "well past 6",
    since postseason week numbers restart at 1 and preseason blending only
    ever needs to know it's no longer in the first 6 weeks. `stage` drives
    the site's "End of Regular Season" / "End of Bowl Season" labels.

    Army-Navy is played the week after conference championships nearly
    every season, and CFBD tags it with its own trailing week number --
    left alone, that becomes the "last regular week" instead of the actual
    championship week, throwing off both the "End of Regular Season" label
    and the committee-rankings week alignment (the real selection ranking
    is tagged championship-week + 1, not Army-Navy-week + 1). Folded into
    the championship week's snapshot instead -- it's still a real result,
    just not its own milestone."""
    regular = [r for r in game_rows if r["season_type"] == "regular" and r["week"] is not None]
    postseason = [r for r in game_rows if r["season_type"] != "regular"]

    weeks = sorted({r["week"] for r in regular})
    trailing_extra = []
    while len(weeks) > 1 and _is_army_navy_only([r for r in regular if r["week"] == weeks[-1]]):
        trailing_extra = [r for r in regular if r["week"] == weeks[-1]] + trailing_extra
        weeks.pop()

    for wk in weeks:
        cumulative = [r for r in regular if r["week"] <= wk]
        if wk == weeks[-1]:
            cumulative = cumulative + trailing_extra
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

        rankings_by_week = fetch_committee_rankings(year, api_key)
        call_count += 1

        # The real playoff field is a fixed, season-level fact (who
        # actually got picked), only knowable at the true final regular
        # week and postseason. Materializing build_snapshots() up front is
        # what lets us know which week that is before the per-snapshot
        # loop below -- every other ranked week instead gets a same-week
        # projection (real_field_projection()), not the exact answer.
        snapshots = list(build_snapshots(game_rows))
        regular_weeks = [wk for (_, _, wk, stage) in snapshots if stage == "regular"]
        final_week = regular_weeks[-1] if regular_weeks else None
        final_committee_ranks = committee_ranks_for_week(rankings_by_week, final_week) if final_week is not None else None

        cfp_participants = fetch_cfp_participants(year, api_key)
        call_count += 1
        exact_seeds = real_playoff_field(year, cfp_participants, final_committee_ranks)

        season_new_rows = []
        for as_of, cumulative, week, stage in snapshots:
            results = compute_ratings(teams, to_rating_games(cumulative))
            blended_raw = blend_raw_ratings(results, prior_raw, week)
            scores = normalize_values(blended_raw)
            committee_ranks = committee_ranks_for_week(rankings_by_week, week)
            is_final_snapshot = stage == "postseason" or week == final_week
            if is_final_snapshot and exact_seeds is not None:
                playoff_field, playoff_seeds = set(exact_seeds.keys()), exact_seeds
            else:
                playoff_field, playoff_seeds = real_field_projection(committee_ranks, teams, year), None
            season_new_rows.extend(build_public_rows(
                results, scores, teams, season=year, as_of=as_of, stage=stage,
                committee_ranks=committee_ranks, playoff_field=playoff_field, playoff_seeds=playoff_seeds,
            ))

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
