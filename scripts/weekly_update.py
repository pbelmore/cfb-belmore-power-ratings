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
import time
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
    """A backfill run makes dozens of these calls, and CFBD occasionally
    returns a transient 502/503 -- retry a few times with backoff before
    giving up, rather than losing an otherwise-successful multi-season run
    to a single flaky response."""
    last_error = None
    for attempt in range(4):
        if attempt:
            time.sleep(2 * attempt)
        try:
            resp = requests.get(
                f"{API_BASE}{path}",
                headers={"Authorization": f"Bearer {api_key}"},
                params={k: v for k, v in params.items() if v is not None},
                timeout=30,
            )
            if resp.status_code in (500, 502, 503, 504):
                last_error = requests.exceptions.HTTPError(f"{resp.status_code} from {path}", response=resp)
                continue
            resp.raise_for_status()
            # Some endpoints return 204 (empty body) rather than [] for a
            # year with no data -- e.g. /playoffs/cfp/participants for any
            # pre-2014 year, since the CFP didn't exist yet.
            return resp.json() if resp.content else []
        except requests.exceptions.RequestException as exc:
            last_error = exc
    raise last_error


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


def fetch_committee_rankings(year, api_key):
    """{cfbd_week: {team: rank}} for that season's real committee rankings
    -- "BCS Standings" pre-2014, "Playoff Committee Rankings" 2014+ -- one
    call for the whole season (CFBD returns every week at once when `week`
    is omitted).

    IMPORTANT: CFBD's own "week N" label on a ranking release always
    reflects results through week (N-1)'s games, not week N's -- verified
    against real game dates for both eras (e.g. the 2024 "week 11" CFP
    ranking was announced Nov 5, before week 11's games even started, so it
    reflects week 10's results; same lag pattern holds for 2010's BCS
    Standings). So a snapshot as-of week N should look up week (N + 1)
    here, not week N: `rankings_by_week.get(week + 1)`. That naturally
    returns nothing for a week before rankings start (early season), and
    for postseason snapshots (week=99 sentinel -- no week 100 ranking will
    ever exist, since the committee's job -- the actual selection -- ends
    once the field is picked)."""
    poll_name = "BCS Standings" if year < 2014 else "Playoff Committee Rankings"
    raw = cfbd_get("/rankings", api_key, year=year)
    by_week = {}
    for entry in raw:
        if entry.get("seasonType") != "regular":
            continue
        for poll in entry.get("polls", []):
            if poll["poll"] == poll_name:
                by_week[entry["week"]] = {r["school"]: r["rank"] for r in poll["ranks"]}
    return by_week


def committee_ranks_for_week(rankings_by_week, week):
    """The real committee ranking for an as-of-week-`week` snapshot: week
    (week + 1) in CFBD's own numbering (see fetch_committee_rankings()),
    falling back to week `week` itself if that's not available. That
    fallback matters for at least one real season: 2020's pandemic-
    compressed schedule had conference championships and the CFP selection
    announcement land in the same week, so CFBD tagged the final/selection
    ranking with that same week number instead of the usual +1 (verified:
    its "week 16" ranking is exactly the real final four -- Alabama, Notre
    Dame, Clemson, Ohio State -- with no "week 17" release at all that
    season)."""
    if week is None:
        return None
    return rankings_by_week.get(week + 1) or rankings_by_week.get(week)


def fetch_cfp_participants(year, api_key):
    """The real CFP field for `year`, via CFBD's dedicated
    /playoffs/cfp/participants endpoint -- {team: seed}. Naturally empty
    before that season's selection actually happens (verified: the
    current not-yet-selected season returns []) and empty for every year
    before the CFP existed (2010-2013 uses the BCS top-2 instead, see
    real_playoff_field())."""
    raw = cfbd_get("/playoffs/cfp/participants", api_key, year=year)
    return {p["team"]["school"]: p["seed"] for p in raw}


def real_playoff_field(year, cfp_participants, final_committee_ranks):
    """The real playoff/BCS field as {team: seed}, or None if that isn't
    determined yet. 2014+: CFP's real field (cfp_participants, empty
    until selection happens -- so this stays None all season until then,
    then flips to the real seeded field). 2010-2013 (no CFP existed): the
    top 2 teams in the final BCS Standings, seeded 1 and 2 by that same
    rank -- the BCS title game was always exactly the top 2, no other
    selection criteria, so rank IS the seed. Needs that season's true
    final ranking (`final_committee_ranks`), not just whatever week is
    current."""
    if year < 2014:
        if not final_committee_ranks:
            return None
        ranked = sorted(final_committee_ranks.items(), key=lambda kv: kv[1])[:2]
        return {team: i + 1 for i, (team, _) in enumerate(ranked)}
    return dict(cfp_participants) if cfp_participants else None


def real_field_projection(committee_ranks, teams, season):
    """"If the real field were picked today," projected from this week's
    real committee ranking -- the same "conference leaders get automatic
    bids" mechanic as our own getPlayoffProjection (index.html), just fed
    CFBD's real ranks instead of our own power_score. "Leader" here means
    "this week's highest-real-ranked team in that conference", a proxy
    for the eventual conference champion, since that isn't actually
    decided until championship week -- so this is necessarily a rougher
    estimate than real_playoff_field()'s ground truth, and callers should
    prefer that whenever it's available (see build_snapshots callers).
    Returns a plain set (no seeds -- those only mean something once the
    real bracket is set), or None if there's no ranking to project from
    yet this week."""
    if not committee_ranks:
        return None
    ranked = sorted(committee_ranks.items(), key=lambda kv: kv[1])
    if season < 2014:
        return {team for team, _ in ranked[:2]}
    if season < 2024:
        return {team for team, _ in ranked[:4]}
    conf_of = {t["school"]: t.get("conference") for t in teams}
    leader_by_conf = {}
    for team, rank in ranked:
        conf = conf_of.get(team)
        if not conf or conf == "FBS Independents":
            continue
        leader_by_conf.setdefault(conf, (team, rank))
    leaders = sorted(leader_by_conf.values(), key=lambda tr: tr[1])[:5]
    leader_names = {team for team, _ in leaders}
    at_large = [team for team, _ in ranked if team not in leader_names][:7]
    return leader_names | set(at_large)


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
    """Highest completed week number, excluding a trailing Army-Navy-only
    week -- it's played the week after conference championships nearly
    every season, and CFBD tags it with its own week number, which would
    otherwise throw off both the preseason-blend weight (harmless, already
    0 by then) and the committee-rankings lookup (not harmless: the real
    selection ranking is tagged championship-week + 1, not
    Army-Navy-week + 1)."""
    weeks = sorted({g["week"] for g in game_rows if g.get("week") is not None})
    if not weeks:
        return 1
    while len(weeks) > 1:
        last_week_games = [g for g in game_rows if g.get("week") == weeks[-1]]
        if all({g["winner"], g["loser"]} == {"Army", "Navy"} for g in last_week_games):
            weeks.pop()
        else:
            break
    return weeks[-1]


def build_public_rows(
    results, scores, teams, season, as_of, stage="regular",
    committee_ranks=None, playoff_field=None, playoff_seeds=None,
):
    """Like ratings_core.build_snapshot_rows(), but persists power_score
    instead of the raw sos/rating fields -- those never touch a committed
    file. Reuses compute_ratings()'s actual math untouched; this only
    changes what gets written. `stage` ("regular"/"postseason") lets the
    site label the last regular-season snapshot and the last
    postseason-inclusive snapshot as season milestones instead of just
    another week. `committee_ranks` ({team: rank}, already resolved to the
    right week by the caller via committee_ranks_for_week()) becomes the
    real BCS/CFP rank shown alongside our own power_score, absent for a
    team not in that week's top 25 or for weeks no ranking applies to.
    `playoff_field` (a set of team names, or None if not yet determined)
    becomes made_playoff: true/false once known, else null -- callers pass
    either the exact field (real_playoff_field(), only known at the true
    final week) or, for every other ranked week, a same-week projection
    (real_field_projection()). `playoff_seeds` ({team: seed}) is only ever
    passed alongside the exact field, giving real bracket position for the
    dashboard's exact-position-match stat; projected weeks leave it None
    since a projection's "seed" isn't a real fact yet."""
    conf_of = {t["school"]: t.get("conference") for t in teams}
    committee_ranks = committee_ranks or {}
    playoff_seeds = playoff_seeds or {}
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
            "committee_rank": committee_ranks.get(team),
            "made_playoff": (team in playoff_field) if playoff_field is not None else None,
            "seed": playoff_seeds.get(team),
        })
    return rows


def playoff_projection(scores, teams, season):
    """Mirrors index.html's getPlayoffProjection: a running "if it ended
    today" field from that snapshot's own scores, not the actual selection.
    2010-2013 (BCS): top 2. 2014-2023 (old CFP): top 4. 2024+: the 12-team
    format's 5 highest-scored conference leaders (auto bids -- independents
    can't have one, no conference championship to win) + the next 7 best
    overall. (Currently only ever called for season >= 2024 -- see
    format_weekly_blurb's own top-10 branch for the pre-2024 blurb -- but
    kept correct for every era rather than leaving dead-but-wrong code.)"""
    conf_of = {t["school"]: t.get("conference") for t in teams}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if season < 2024:
        field_size = 2 if season < 2014 else 4
        return {team for team, _ in ranked[:field_size]}
    leader_by_conf = {}
    for team, score in ranked:
        conf = conf_of.get(team)
        if not conf or conf == "FBS Independents":
            continue
        leader_by_conf.setdefault(conf, (team, score))
    leaders = sorted(leader_by_conf.values(), key=lambda ts: ts[1], reverse=True)[:5]
    leader_names = {team for team, _ in leaders}
    at_large = [team for team, _ in ranked if team not in leader_names][:7]
    return leader_names | set(at_large)


def format_weekly_blurb(results, scores, teams, season, title=None):
    # Rank by the same (possibly blended) scores being displayed -- sorting
    # by raw rating instead would let the blend reorder teams without the
    # printed rank agreeing with the printed number.
    ranked = sorted(results.items(), key=lambda kv: scores[kv[0]], reverse=True)
    if season < 2024:
        # What actually got posted pre-2024: top 10, not a full top 25.
        selected = {team for team, _ in ranked[:10]}
    else:
        # From 2024 on, just the projected playoff field -- true rank
        # number kept, so an auto-bid conference champ ranked outside the
        # top 12 overall still shows its real position (e.g. 1-10, 14, 16).
        selected = playoff_projection(scores, teams, season)
    lines = [title or "Power Ratings"]
    for i, (team, r) in enumerate(ranked, start=1):
        if team not in selected:
            continue
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
    current_week = current_week_number(game_rows)
    blended_raw = blend_raw_ratings(results, prior_raw, current_week)
    scores = normalize_values(blended_raw)
    stage = current_stage(game_rows)

    print("Fetching committee rankings...")
    rankings_by_week = fetch_committee_rankings(args.year, api_key)
    committee_ranks = committee_ranks_for_week(rankings_by_week, current_week)
    print(f"  {len(committee_ranks) if committee_ranks else 0} teams ranked this week")

    print("Fetching real playoff field...")
    cfp_participants = fetch_cfp_participants(args.year, api_key)
    exact_seeds = real_playoff_field(args.year, cfp_participants, committee_ranks)
    if exact_seeds is not None:
        playoff_field, playoff_seeds = set(exact_seeds.keys()), exact_seeds
        print(f"  {len(playoff_field)} teams (real field determined)")
    else:
        playoff_field, playoff_seeds = real_field_projection(committee_ranks, teams, args.year), None
        print(f"  {len(playoff_field) if playoff_field else 0} teams (projected -- real field not determined yet)")

    new_rows = build_public_rows(
        results, scores, teams, season=args.year, as_of=as_of, stage=stage,
        committee_ranks=committee_ranks, playoff_field=playoff_field, playoff_seeds=playoff_seeds,
    )

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

    blurb = format_weekly_blurb(results, scores, teams, args.year, title=f"CFB Power Ratings -- {args.year} as of {as_of}")
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
