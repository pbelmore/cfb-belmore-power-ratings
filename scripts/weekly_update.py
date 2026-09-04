#!/usr/bin/env python3
"""
weekly_update.py -- pulls this season's FBS teams and completed games from
the CFBD API, runs them through ratings_core, and writes:

  data/teams.json            current-season roster (school, conference)
  data/games.json             one row per completed game
  data/ratings_history.json   long-format, one row per team per run (appended)
  data/weekly_blurb.txt       plain-text top-25, ready to paste into a group chat
  data/team_colors.json       {team: {color, altColor}} for every team ever seen

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
            "season_type": g.get("seasonType"),
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


def fetch_team_colors(api_key, team_names):
    """CFBD's /teams (no year filter) covers every team it has ever tracked,
    across all classifications and eras -- one call gets colors for the
    whole history in one shot. Filtered down to `team_names` so the
    committed file only carries teams we actually reference."""
    raw = cfbd_get("/teams", api_key)
    wanted = set(team_names)
    colors = {}
    for t in raw:
        if t["school"] in wanted and t.get("color"):
            colors[t["school"]] = {
                "color": t["color"],
                "altColor": t.get("alternateColor"),
            }
    return colors


def current_stage(game_rows):
    """"postseason" once any completed game this pull is a bowl/CFP game,
    else "regular". Drives the site's "End of Regular Season" / "End of
    Bowl Season" labels for the last snapshot of each kind."""
    if any(g.get("season_type") == "postseason" for g in game_rows):
        return "postseason"
    return "regular"


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


def normalize_values(values_by_team):
    """0-100 rescale, best team = 100. The one place normalization happens --
    called on (possibly preseason-blended) raw ratings, never on an
    already-normalized number, so blending twice can't cap the result below
    100 just because one team is clearly on top of a still-thin field."""
    values = list(values_by_team.values())
    lo, hi = min(values), max(values)
    if hi == lo:
        return {team: 0.0 for team in values_by_team}
    return {team: (v - lo) / (hi - lo) * 100 for team, v in values_by_team.items()}


def preseason_blend_weight(week):
    """Linear ramp: 50% weight on last season's final rating at week 1, down
    to 0% (pure current-season) by week 7. week=None/0 (preseason, no games
    yet) gets the full week-1 weight."""
    week = week or 1
    if week >= 7:
        return 0.0
    return 0.5 * (7 - week) / 6


def prior_season_raw_ratings(history, year, api_key):
    """Recomputes last season's FINAL raw ratings (the actual SOS x Win%^2
    values, not the normalized power_score) fresh via 2 CFBD calls -- these
    are never cached or written anywhere, since only the normalized score is
    supposed to persist. Blending on the raw number, not the normalized one,
    matters: the normalized score always tops out at 100 for whoever won,
    regardless of how dominant that team's season actually was, so it's
    already lost the information a blend should be using.

    Returns None if `year - 1` isn't a season we track at all (gated on our
    own history, not on whether CFBD happens to have even older data) --
    that's what keeps the earliest tracked season from blending against
    anything."""
    if not any(r["season"] == year - 1 for r in history):
        return None
    prior_teams = fetch_teams(year - 1, api_key)
    prior_games = fetch_games(year - 1, api_key)
    if not prior_games:
        return None
    prior_results = compute_ratings(prior_teams, to_rating_games(prior_games))
    return {team: r["rating"] for team, r in prior_results.items()}


def blend_raw_ratings(results, prior_raw_ratings, week):
    """Blends each team's raw rating toward last season's raw final rating.
    Returns a plain {team: value} dict of blended raw ratings, still to be
    normalized afterward. A team missing from prior_raw_ratings (new to
    FBS, say) is left at its current-season raw rating."""
    if not prior_raw_ratings:
        return {team: r["rating"] for team, r in results.items()}
    w = preseason_blend_weight(week)
    if w <= 0:
        return {team: r["rating"] for team, r in results.items()}
    return {
        team: w * prior_raw_ratings[team] + (1 - w) * r["rating"] if team in prior_raw_ratings else r["rating"]
        for team, r in results.items()
    }


def current_week_number(game_rows):
    weeks = [g["week"] for g in game_rows if g.get("week") is not None]
    return max(weeks) if weeks else 1


def build_public_rows(results, scores, teams, season, as_of, stage="regular"):
    """Like ratings_core.build_snapshot_rows(), but persists power_score
    instead of the raw sos/rating fields -- those never touch a committed
    file. Reuses compute_ratings()'s actual math untouched; this only
    changes what gets written. `stage` ("regular"/"postseason") lets the
    site label the last regular-season snapshot and the last
    postseason-inclusive snapshot as season milestones instead of just
    another week."""
    conf_of = {t["school"]: t.get("conference") for t in teams}
    rows = []
    for team, r in results.items():
        rows.append({
            "season": season,
            "as_of": as_of,
            "stage": stage,
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
    prior_raw = prior_season_raw_ratings(history, args.year, api_key)
    blended_raw = blend_raw_ratings(results, prior_raw, current_week_number(game_rows))
    scores = normalize_values(blended_raw)
    stage = current_stage(game_rows)
    new_rows = build_public_rows(results, scores, teams, season=args.year, as_of=as_of, stage=stage)

    print("Fetching team colors...")
    all_team_names = {t["school"] for t in teams} | {r["team"] for r in history}
    colors = fetch_team_colors(api_key, all_team_names)
    write_json(out_dir / "team_colors.json", colors)
    print(f"  {len(colors)} teams with colors")

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

    print(
        f"\nWrote {out_dir}/teams.json, {out_dir}/games.json, {out_dir}/ratings_history.json, "
        f"{out_dir}/weekly_blurb.txt, {out_dir}/team_colors.json"
    )


if __name__ == "__main__":
    main()
