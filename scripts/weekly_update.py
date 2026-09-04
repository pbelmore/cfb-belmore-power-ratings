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
SITE_URL = "https://pbelmore.github.io/cfb-belmore-power-ratings/"

# Game.location codes <-> the human-readable words stored in games.json
CODE_TO_WORD = {"": "home", "@": "away", "N": "neutral"}
WORD_TO_CODE = {v: k for k, v in CODE_TO_WORD.items()}

# Playoff-format era boundaries -- the one place these two cutoffs live.
# CFP_ERA_START: first season with a real committee-selected field (CFP)
# instead of the BCS's top-2-by-standings title game.
# TWELVE_TEAM_ERA_START: first season of the 12-team format (5 conference-
# leader auto bids + 7 at-large) instead of the old flat top-4.
CFP_ERA_START = 2014
TWELVE_TEAM_ERA_START = 2024


def cfbd_get(path, api_key, **params):
    """A backfill run makes dozens of these calls, and CFBD occasionally
    returns a transient 502/503 -- retry a few times with backoff before
    giving up, rather than losing an otherwise-successful multi-season run
    to a single flaky response. Only transient failures (5xx, or a network-
    level connection/timeout error) are retried -- a permanent error like a
    401 (bad API key) or 404 raises immediately instead of burning three
    more rounds of backoff on something retrying can never fix."""
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
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
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


def season_week_label(current_week, stage, conf_champions, game_rows):
    """The Slack blurb's title suffix: "Week N" during the regular season,
    "End of Regular Season" once conference championships have been played
    (they're always the last week of the regular season, so a non-empty
    `conf_champions` means that week is done), "Bowl Season" once
    postseason games have started, "End of Bowl Season" once the actual
    final game of the season -- the CFP National Championship, detected
    via CFBD's `notes` field the same way conference_champions() detects
    title games -- has been played."""
    if stage == "postseason":
        is_final = any(
            g.get("season_type") == "postseason" and "national championship" in (g.get("notes") or "").lower()
            for g in game_rows
        )
        return "End of Bowl Season" if is_final else "Bowl Season"
    if conf_champions:
        return "End of Regular Season"
    return f"Week {current_week}"


# CFBD's own raw data for the 2023 season's final ("week 15") Playoff
# Committee Rankings release ties Florida State and Georgia at rank 5 and
# skips rank 6 entirely -- verified against the committee's own final
# rankings announcement (collegefootballplayoff.com, 2023-12-03), which has
# them distinct: Florida State 5, Georgia 6. Every other rank in that
# release (1-4, 7-25) matches CFBD's data exactly, so this is a one-off
# glitch in CFBD's data for this single release, not a real committee tie
# or a wrong week being read here. Keyed by (year, week, team) so a fix is
# scoped to the exact release it was observed in.
KNOWN_RANK_FIXES = {
    (2023, 15, "Florida State"): 5,
    (2023, 15, "Georgia"): 6,
}


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
    poll_name = "BCS Standings" if year < CFP_ERA_START else "Playoff Committee Rankings"
    raw = cfbd_get("/rankings", api_key, year=year)
    by_week = {}
    for entry in raw:
        if entry.get("seasonType") != "regular":
            continue
        for poll in entry.get("polls", []):
            if poll["poll"] == poll_name:
                ranks = {r["school"]: r["rank"] for r in poll["ranks"]}
                for team in ranks:
                    fix = KNOWN_RANK_FIXES.get((year, entry["week"], team))
                    if fix is not None:
                        ranks[team] = fix
                by_week[entry["week"]] = ranks
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
    if (week + 1) in rankings_by_week:
        return rankings_by_week[week + 1]
    return rankings_by_week.get(week)


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
    if year < CFP_ERA_START:
        if not final_committee_ranks:
            return None
        ranked = sorted(final_committee_ranks.items(), key=lambda kv: kv[1])[:2]
        return {team: i + 1 for i, (team, _) in enumerate(ranked)}
    return dict(cfp_participants) if cfp_participants else None


def conference_champions(game_rows, teams):
    """{conference: team} for every conference whose championship game has
    already been played in `game_rows` -- detected via CFBD's `notes`
    field, which reliably reads "<name> Championship" for these games
    (verified for the 2024 and 2025 seasons: exactly one such game per
    conference-with-a-championship in the season's final regular-season
    week, e.g. "SEC Championship", "Dr Pepper Big 12 Championship"). Keyed
    by the winner's own conference (from `teams`), not text parsed out of
    the notes, so sponsor-name/abbreviation quirks in that text don't
    matter. Empty before championship week is played; only meaningful for
    the 12-team era's conference-leader auto bids -- see
    conference_leader_field()."""
    conf_of = {t["school"]: t.get("conference") for t in teams}
    champions = {}
    for g in game_rows:
        if g.get("season_type") != "regular":
            continue
        if "championship" not in (g.get("notes") or "").lower():
            continue
        conf = conf_of.get(g["winner"])
        if conf:
            champions[conf] = g["winner"]
    return champions


def conference_leaders_and_at_large(ranked_teams, teams, leaders=5, at_large=7, conf_champions=None):
    """The 12-team format's "5 conference-leader auto bids + 7 at-large"
    selection rule, shared by every playoff-field projection in this
    module (real_field_projection fed committee ranks, playoff_projection
    fed our own scores). `ranked_teams` is [(team, value)] already sorted
    best-first; independents are excluded from auto bids since there's no
    conference championship to win one. Returns (leader_teams,
    at_large_teams) as two separate ordered lists (best-first) rather than
    one merged set, so a caller that needs to tell a conference-leader auto
    bid apart from an at-large team (e.g. format_slack_blurb's "*") doesn't
    have to re-derive it -- conference_leader_field() below is just this
    with the two unioned, for callers that only need the combined field.

    `conf_champions` ({conference: team}, from conference_champions())
    overrides the "best-ranked/-scored team in that conference" stand-in
    with the actual championship-game winner for any conference whose
    title game has already been played -- the two aren't always the same
    team (e.g. Clemson beating a higher-ranked SMU for the 2024 ACC title,
    then correctly holding the ACC's auto bid over SMU). A conference
    whose title game hasn't been played yet (or that has none) still falls
    back to the rank-based stand-in -- the only option before that
    conference's champion is actually decided.

    The 5 auto-bid conferences are chosen by their leader's own rank
    (best-first), not by whichever team first put that conference on the
    board while scanning `ranked_teams` -- those can differ once
    `conf_champions` swaps in a champion who isn't that conference's
    highest-ranked team."""
    conf_of = {t["school"]: t.get("conference") for t in teams}
    conf_champions = conf_champions or {}
    rank_of = {team: i for i, (team, _) in enumerate(ranked_teams)}

    conf_leader = {}
    for team, _ in ranked_teams:
        conf = conf_of.get(team)
        if not conf or conf == "FBS Independents" or conf in conf_leader:
            continue
        conf_leader[conf] = team
    for conf, champ in conf_champions.items():
        if conf in conf_leader and champ in rank_of:
            conf_leader[conf] = champ

    ordered_leaders = sorted(conf_leader.values(), key=lambda t: rank_of[t])
    leader_teams = ordered_leaders[:leaders]
    leader_names = set(leader_teams)
    at_large_teams = [team for team, _ in ranked_teams if team not in leader_names][:at_large]
    return leader_teams, at_large_teams


def conference_leader_field(ranked_teams, teams, leaders=5, at_large=7, conf_champions=None):
    """The 12-team field as a single set -- see
    conference_leaders_and_at_large() for the leader/at-large split."""
    leader_teams, at_large_teams = conference_leaders_and_at_large(
        ranked_teams, teams, leaders=leaders, at_large=at_large, conf_champions=conf_champions,
    )
    return set(leader_teams) | set(at_large_teams)


def playoff_field_from_ranked(ranked_teams, teams, season, conf_champions=None):
    """The full "who's in" rule for `season`, given `ranked_teams` already
    sorted best-first: top-2 pre-CFP (BCS), top-4 old-CFP, else the 12-team
    conference-leader + at-large field. Shared by real_field_projection
    (fed committee ranks) and playoff_projection (fed our own scores) so
    the era cutoffs and selection rule live in exactly one place."""
    if season < CFP_ERA_START:
        return {team for team, _ in ranked_teams[:2]}
    if season < TWELVE_TEAM_ERA_START:
        return {team for team, _ in ranked_teams[:4]}
    return conference_leader_field(ranked_teams, teams, conf_champions=conf_champions)


def real_field_projection(committee_ranks, teams, season, conf_champions=None):
    """"If the real field were picked today," projected from this week's
    real committee ranking -- the same "conference leaders get automatic
    bids" mechanic as our own getPlayoffProjection (index.html), just fed
    CFBD's real ranks instead of our own power_score. "Leader" here means
    "this week's highest-real-ranked team in that conference" unless
    `conf_champions` (from conference_champions()) already knows the real
    champion for it, a proxy for the eventual conference champion before
    that's decided -- so absent `conf_champions`, this is necessarily a
    rougher estimate than real_playoff_field()'s ground truth, and callers
    should prefer that whenever it's available (see build_snapshots
    callers). Returns a plain set (no seeds -- those only mean something
    once the real bracket is set), or None if there's no ranking to
    project from yet this week."""
    if not committee_ranks:
        return None
    ranked = sorted(committee_ranks.items(), key=lambda kv: kv[1])
    return playoff_field_from_ranked(ranked, teams, season, conf_champions=conf_champions)


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


def merge_history_rows(history, new_rows):
    """Replace any existing `history` rows sharing a (season, as_of) with
    something in `new_rows`, append `new_rows`, and re-sort deterministically.
    The (season, as_of) pairs to replace are derived from `new_rows` itself,
    so this handles both a single-snapshot weekly run and a multi-snapshot
    backfill run uniformly -- re-running the same day's snapshot (or a
    whole season's backfill) updates it in place instead of duplicating it.

    Sorted afterward because compute_ratings() iterates a set() internally,
    whose order varies per process (Python string-hash randomization), so
    without this every run would rewrite the whole file's line order."""
    keys_this_run = {(r["season"], r["as_of"]) for r in new_rows}
    history = [r for r in history if (r["season"], r["as_of"]) not in keys_this_run]
    history.extend(new_rows)
    history.sort(key=lambda r: (r["season"], r["as_of"], r["team"]))
    return history


def load_json(path, default):
    if path.exists():
        with path.open() as f:
            return json.load(f)
    return default


def write_json(path, data):
    """Writes to a temp file and renames it into place, so a process kill
    or crash mid-write can never leave `path` truncated/invalid for the
    next run (or the live site, which fetches these files directly) to
    trip over."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    tmp_path.replace(path)


def normalize_values(values_by_team):
    """0-100 rescale, best team = 100. The one place normalization happens --
    called on (possibly preseason-blended) raw ratings, never on an
    already-normalized number, so blending twice can't cap the result below
    100 just because one team is clearly on top of a still-thin field."""
    if not values_by_team:
        return {}
    values = list(values_by_team.values())
    lo, hi = min(values), max(values)
    if hi == lo:
        # Every team tied (e.g. week 1 of the earliest tracked season, no
        # prior-year blend to draw on -- every team's raw rating is 0.0).
        # No team is actually "below" any other here, so nobody should read
        # as trailing the field -- give everyone the top score instead of
        # the 0.0 an unguarded rescale would produce.
        return {team: 100.0 for team in values_by_team}
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
    return raw_ratings_from_data(prior_teams, prior_games)


def raw_ratings_from_data(teams, game_rows):
    """The actual final raw ratings (SOS x Win%^2, not the normalized
    power_score) for a season, given its teams/games already fetched.
    Split out of prior_season_raw_ratings() so a caller that already has a
    season's data in hand (backfill_history.py's per-year loop, reusing
    what the previous iteration fetched) can reuse it instead of
    re-fetching the same teams/games from CFBD a second time."""
    if not game_rows:
        return None
    results = compute_ratings(teams, to_rating_games(game_rows))
    return {team: r["rating"] for team, r in results.items()}


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


def is_trailing_army_navy_week(games_in_week):
    """True if every game in this week's slate is Army-Navy -- it's played
    the week after conference championships nearly every season, and CFBD
    tags it with its own trailing week number. Shared by
    current_week_number() (drop it from "current") and
    backfill_history.build_snapshots() (fold it into the prior week's
    snapshot instead of giving it a milestone of its own)."""
    return bool(games_in_week) and all({g["winner"], g["loser"]} == {"Army", "Navy"} for g in games_in_week)


def current_week_number(game_rows):
    """Highest completed REGULAR-season week number, excluding a trailing
    Army-Navy-only week -- leaving it in would otherwise throw off both the
    preseason-blend weight (harmless, already 0 by then) and the
    committee-rankings lookup (not harmless: the real selection ranking is
    tagged championship-week + 1, not Army-Navy-week + 1).

    Restricted to season_type == "regular" before any of that: postseason
    week numbers restart at 1, so mixing them in relies on postseason weeks
    always staying below the regular-season max -- true in every season
    seen so far, but not a real guarantee, so filtering explicitly avoids
    depending on it."""
    regular_rows = [g for g in game_rows if g.get("season_type") == "regular"]
    weeks = sorted({g["week"] for g in regular_rows if g.get("week") is not None})
    if not weeks:
        return 1
    while len(weeks) > 1:
        last_week_games = [g for g in regular_rows if g.get("week") == weeks[-1]]
        if is_trailing_army_navy_week(last_week_games):
            weeks.pop()
        else:
            break
    return weeks[-1]


def build_public_rows(
    results, scores, teams, season, as_of, stage="regular",
    committee_ranks=None, playoff_field=None, playoff_seeds=None, conf_champions=None,
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
    since a projection's "seed" isn't a real fact yet. `conf_champions`
    ({conference: team}, from conference_champions()) becomes each row's
    `conf_champion` flag -- true only for that snapshot's actual
    conference-championship-game winner, false otherwise (always a known
    fact once game data is in, never null) -- so the client-side "our
    projection" logic (index.html/accuracy.html) can apply the same
    real-champion override as conference_leader_field() without needing
    game data of its own."""
    conf_of = {t["school"]: t.get("conference") for t in teams}
    committee_ranks = committee_ranks or {}
    playoff_seeds = playoff_seeds or {}
    champion_teams = set((conf_champions or {}).values())
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
            "conf_champion": team in champion_teams,
        })
    return rows


def playoff_projection(scores, teams, season, conf_champions=None):
    """Mirrors index.html's getPlayoffProjection: a running "if it ended
    today" field from that snapshot's own scores, not the actual selection.
    2010-2013 (BCS): top 2. 2014-2023 (old CFP): top 4. 2024+: the 12-team
    format's 5 highest-scored conference leaders (auto bids -- independents
    can't have one, no conference championship to win) + the next 7 best
    overall. (Currently only ever called for season >= 2024 -- see
    format_weekly_blurb's own top-10 branch for the pre-2024 blurb -- but
    kept correct for every era rather than leaving dead-but-wrong code.)"""
    # Team name is a secondary sort key so a tie (common -- power_score is
    # rounded to 1 decimal) breaks the same way on every run. Without it,
    # ties break on `scores`' insertion order, which inherits
    # compute_ratings()'s internal set() iteration -- hash-randomized per
    # Python process -- so the same underlying data could pick a different
    # team for the last at-large/conference-leader slot from one run to the
    # next, disagreeing with the deterministically-sorted JSON the JS-side
    # projections (index.html/accuracy.html) read back.
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return playoff_field_from_ranked(ranked, teams, season, conf_champions=conf_champions)


def format_weekly_blurb(results, scores, teams, season, title=None, conf_champions=None):
    # Rank by the same (possibly blended) scores being displayed -- sorting
    # by raw rating instead would let the blend reorder teams without the
    # printed rank agreeing with the printed number. Team name as a
    # secondary key for the same reason as playoff_projection() above --
    # deterministic tie-breaking regardless of dict iteration order.
    ranked = sorted(results.items(), key=lambda kv: (-scores[kv[0]], kv[0]))
    if season < TWELVE_TEAM_ERA_START:
        # What actually got posted pre-2024: top 10, not a full top 25.
        selected = {team for team, _ in ranked[:10]}
    else:
        # From 2024 on, just the projected playoff field -- true rank
        # number kept, so an auto-bid conference champ ranked outside the
        # top 12 overall still shows its real position (e.g. 1-10, 14, 16).
        selected = playoff_projection(scores, teams, season, conf_champions=conf_champions)
    lines = [title or "Power Ratings"]
    for i, (team, r) in enumerate(ranked, start=1):
        if team not in selected:
            continue
        lines.append(f"{i}. {team} ({r['wins']}-{r['losses']})")
    return "\n".join(lines)


def format_slack_blurb(results, scores, teams, season, week_label, conf_champions=None, link=None):
    """The Slack-posted version of the blurb -- 12-team-era only (we'll
    never post this for a past pre-2024 season, so there's no top-10/top-4/
    top-2 branching here like format_weekly_blurb has). Uses Slack's
    mrkdwn syntax (*bold*, _italic_), not plain text or real Markdown --
    this is only ever meant to go straight into a Slack message, unlike
    format_weekly_blurb's output. Title is "Week N" (or "End of Regular
    Season"/"Bowl Season"/"End of Bowl Season" -- see season_week_label())
    instead of a date, since a Slack reader cares what week this is, not
    which calendar day it happened to run. Team lines get a "*" marking
    the 5 conference-leader auto bids (vs. the 7 at-large teams); the
    footnote explaining that and the closing link are italicized so they
    read as asides rather than part of the ranking itself."""
    ranked = sorted(results.items(), key=lambda kv: (-scores[kv[0]], kv[0]))
    ranked_by_score = [(team, scores[team]) for team, _ in ranked]
    leader_teams, at_large_teams = conference_leaders_and_at_large(ranked_by_score, teams, conf_champions=conf_champions)
    leader_names = set(leader_teams)
    selected = leader_names | set(at_large_teams)

    lines = [f"*Belmore Rankings {season} - {week_label}*"]
    for i, (team, r) in enumerate(ranked, start=1):
        if team not in selected:
            continue
        marker = " *" if team in leader_names else ""
        lines.append(f"{i}. {team} ({r['wins']}-{r['losses']}){marker}")
    lines.append("")
    lines.append("_* = conference leader/champion (automatic bid)_")
    if link:
        lines.append(f"_Full rankings available at {link}_")
    return "\n".join(lines)


def post_to_slack(webhook_url, text):
    resp = requests.post(webhook_url, json={"text": text}, timeout=15)
    resp.raise_for_status()


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
    if not teams:
        sys.exit(f"No FBS teams returned for year {args.year} -- check --year and try again.")

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
    conf_champions = conference_champions(game_rows, teams)

    print("Fetching committee rankings...")
    rankings_by_week = fetch_committee_rankings(args.year, api_key)
    committee_ranks = committee_ranks_for_week(rankings_by_week, current_week)
    print(f"  {len(committee_ranks) if committee_ranks else 0} teams ranked this week")

    print("Fetching real playoff field...")
    cfp_participants = fetch_cfp_participants(args.year, api_key)
    if args.year >= CFP_ERA_START:
        exact_seeds = real_playoff_field(args.year, cfp_participants, committee_ranks)
    else:
        # real_playoff_field()'s pre-CFP (BCS) branch needs that season's
        # TRUE FINAL committee ranking, not just whatever week is current --
        # this script is the live current-season puller and (unlike
        # backfill_history.py) never computes a true final week, so
        # `committee_ranks` here is just this week's ranking. Safe to skip
        # entirely: this script is never actually run against a pre-CFP
        # season (that's what backfill_history.py is for).
        exact_seeds = None
    if exact_seeds is not None:
        playoff_field, playoff_seeds = set(exact_seeds.keys()), exact_seeds
        print(f"  {len(playoff_field)} teams (real field determined)")
    else:
        playoff_field, playoff_seeds = real_field_projection(committee_ranks, teams, args.year, conf_champions=conf_champions), None
        print(f"  {len(playoff_field) if playoff_field else 0} teams (projected -- real field not determined yet)")

    new_rows = build_public_rows(
        results, scores, teams, season=args.year, as_of=as_of, stage=stage,
        committee_ranks=committee_ranks, playoff_field=playoff_field, playoff_seeds=playoff_seeds,
        conf_champions=conf_champions,
    )

    print("Fetching team colors...")
    all_team_names = {t["school"] for t in teams} | {r["team"] for r in history}
    colors = fetch_team_colors(api_key, all_team_names)
    write_json(out_dir / "team_colors.json", colors)
    print(f"  {len(colors)} teams with colors")

    history = merge_history_rows(history, new_rows)
    write_json(history_path, history)

    blurb = format_weekly_blurb(
        results, scores, teams, args.year, title=f"CFB Power Ratings -- {args.year} as of {as_of}",
        conf_champions=conf_champions,
    )
    (out_dir / "weekly_blurb.txt").write_text(blurb + "\n")

    # Slack posting only makes sense for the 12-team era -- we'll never
    # run this script against a past pre-2024 season, so there's no need
    # for format_slack_blurb to handle the older top-10/top-4/top-2 eras.
    # SLACK_WEBHOOK_URL is optional: unset (e.g. a local manual run) just
    # skips posting rather than erroring.
    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if args.year >= TWELVE_TEAM_ERA_START and slack_webhook:
        print("Posting to Slack...")
        week_label = season_week_label(current_week, stage, conf_champions, game_rows)
        slack_text = format_slack_blurb(
            results, scores, teams, args.year, week_label, conf_champions=conf_champions, link=SITE_URL,
        )
        post_to_slack(slack_webhook, slack_text)

    top = sorted(results.items(), key=lambda kv: (-scores[kv[0]], kv[0]))[:5]
    print(f"\nTop 5 as of {as_of}:")
    for i, (team, r) in enumerate(top, start=1):
        print(f"  {i}. {team} ({r['wins']}-{r['losses']}) -- {scores[team]:.1f}")

    print(
        f"\nWrote {out_dir}/teams.json, {out_dir}/games.json, {out_dir}/ratings_history.json, "
        f"{out_dir}/weekly_blurb.txt, {out_dir}/team_colors.json"
    )


if __name__ == "__main__":
    main()
