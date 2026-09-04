#!/usr/bin/env python3
"""
weekly_update.py -- pulls this season's FBS teams and completed games from
the CFBD API, runs them through ratings_core, and writes:

  data/teams.json            current-season roster (school, conference)
  data/games.json             one row per completed game
  data/ratings_history.json   long-format, one row per team per run (appended)
  data/weekly_blurb.txt       plain-text top-25, ready to paste into a group chat

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
from ratings_core import Game, compute_ratings

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
    teams = [{"school": t["school"], "conference": t.get("conference")} for t in raw]
    teams.sort(key=lambda t: t["school"])
    return teams


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
    # Sort deterministically -- the CFBD API doesn't guarantee stable ordering
    # between calls for games sharing a timestamp, which would otherwise cause
    # a spurious diff/commit every run even when no game data actually changed.
    games.sort(key=lambda r: (r["date"] or "", r["winner"], r["loser"]))
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


def normalized_scores(results):
    """Maps each team's raw rating to a 0-100 score, best team in this run's
    field = 100. This -- not the raw rating or SOS -- is what's ever written
    to public-facing output (ratings_history.json, the blurb, this script's
    own console/CI-log output), so the SOS x Win%^2 formula stays private."""
    ratings = [r["rating"] for r in results.values()]
    lo, hi = min(ratings), max(ratings)
    if hi == lo:
        return {team: 0.0 for team in results}
    return {team: (r["rating"] - lo) / (hi - lo) * 100 for team, r in results.items()}


def preseason_blend_weight(week):
    """Linear ramp: 50% weight on last season's final score at week 1, down
    to 0% (pure current-season) by week 7. week=None/0 (preseason, no games
    yet) gets the full week-1 weight."""
    week = week or 1
    if week >= 7:
        return 0.0
    return 0.5 * (7 - week) / 6


def prior_season_final_scores(history, season):
    """team -> power_score from `season - 1`'s last snapshot, or None if that
    season isn't in history at all (e.g. season is the earliest one tracked)."""
    prior_rows = [r for r in history if r["season"] == season - 1]
    if not prior_rows:
        return None
    last_as_of = max(r["as_of"] for r in prior_rows)
    return {r["team"]: r["power_score"] for r in prior_rows if r["as_of"] == last_as_of}


def apply_preseason_blend(scores, prior_scores, week):
    """Blends this week's power scores toward last season's final scores.
    A team missing from prior_scores (new to FBS, say) is left alone."""
    if not prior_scores:
        return scores
    w = preseason_blend_weight(week)
    if w <= 0:
        return scores
    return {
        team: round(w * prior_scores[team] + (1 - w) * score, 1) if team in prior_scores else score
        for team, score in scores.items()
    }


def current_week_number(game_rows):
    weeks = [g["week"] for g in game_rows if g.get("week") is not None]
    return max(weeks) if weeks else 1


def build_public_rows(results, scores, teams, season, as_of):
    """Like ratings_core.build_snapshot_rows(), but persists power_score
    instead of the raw sos/rating fields -- those never touch a committed
    file. Reuses compute_ratings()'s actual math untouched; this only
    changes what gets written."""
    conf_of = {t["school"]: t.get("conference") for t in teams}
    rows = []
    for team, r in results.items():
        rows.append({
            "season": season,
            "as_of": as_of,
            "team": team,
            "conference": conf_of.get(team),
            "wins": r["wins"],
            "losses": r["losses"],
            "win_pct": r["win_pct"],
            "power_score": round(scores[team], 1),
        })
    return rows


def format_weekly_blurb(results, scores, top_n=25, title=None):
    # Rank by the same (possibly blended) scores being displayed -- sorting
    # by raw rating instead would let the blend reorder teams without the
    # printed rank agreeing with the printed number.
    ranked = sorted(results.items(), key=lambda kv: scores[kv[0]], reverse=True)[:top_n]
    lines = [title or "Power Ratings"]
    for i, (team, r) in enumerate(ranked, start=1):
        lines.append(f"{i}. {team} ({r['wins']}-{r['losses']}) -- {scores[team]:.1f}")
    return "\n".join(lines)


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

    history_path = out_dir / "ratings_history.json"
    history = load_json(history_path, [])

    print("Computing ratings...")
    results = compute_ratings(teams, to_rating_games(game_rows))
    scores = normalized_scores(results)
    prior_scores = prior_season_final_scores(history, args.year)
    scores = apply_preseason_blend(scores, prior_scores, current_week_number(game_rows))
    new_rows = build_public_rows(results, scores, teams, season=args.year, as_of=as_of)

    # Replace any existing rows for this (season, as_of) so re-running the
    # same day's snapshot updates it in place instead of duplicating it.
    history = [r for r in history if not (r["season"] == args.year and r["as_of"] == as_of)]
    history.extend(new_rows)
    # Sort deterministically -- compute_ratings() iterates a set() internally,
    # whose order varies per process (Python string-hash randomization), so
    # without this every run would rewrite the whole file's line order.
    history.sort(key=lambda r: (r["season"], r["as_of"], r["team"]))
    write_json(history_path, history)

    blurb = format_weekly_blurb(results, scores, title=f"CFB Power Ratings -- {args.year} as of {as_of}")
    (out_dir / "weekly_blurb.txt").write_text(blurb + "\n")

    top = sorted(results.items(), key=lambda kv: scores[kv[0]], reverse=True)[:5]
    print(f"\nTop 5 as of {as_of}:")
    for i, (team, r) in enumerate(top, start=1):
        print(f"  {i}. {team} ({r['wins']}-{r['losses']}) -- {scores[team]:.1f}")

    print(f"\nWrote {out_dir}/teams.json, {out_dir}/games.json, {out_dir}/ratings_history.json, {out_dir}/weekly_blurb.txt")


if __name__ == "__main__":
    main()
