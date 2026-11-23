#!/usr/bin/env python3
"""
weekly_update.py -- pulls this season's FBS teams and completed games from
the CFBD API, runs them through ratings_core, and writes:

  data/teams.json            current-season roster (school, conference)
  data/games.json             one row per completed game
  data/games_archive.json     every season's FBS-vs-FBS games (this season's replaced each run)
  data/ratings_history.json   long-format, one row per team per run (appended)
  data/weekly_blurb.txt       plain-text top 10 (pre-2024) or projected playoff field, ready to paste
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

# Playoff-format era boundaries and 12-team selection rules -- the one
# place these live on the Python side (playoff-format.js mirrors them).
# CFP_ERA_START: first season with a real committee-selected field (CFP)
# instead of the BCS's top-2-by-standings title game.
# TWELVE_TEAM_ERA_START: first season of the 12-team format (5 conference-
# leader auto bids + 7 at-large) instead of the old flat top-4.
# P4_AUTO_BID_ERA_START: first season of the 2026+ 12-team rules -- all four
# Power Four champions get auto bids regardless of rank (plus the single
# highest-ranked Group of Six team, champion or not), replacing 2024-2025's "five
# highest-ranked conference champions", and Notre Dame is guaranteed a bid
# if it finishes in the top 12 (NOTRE_DAME_TOP_N).
CFP_ERA_START = 2014
TWELVE_TEAM_ERA_START = 2024
P4_AUTO_BID_ERA_START = 2026
POWER_FOUR = {"ACC", "Big Ten", "Big 12", "SEC"}
NOTRE_DAME_TOP_N = 12


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


def fetch_raw_games(year, api_key):
    """Every game on CFBD's schedule for `year`, finished or not, in CFBD's
    own shape -- parse_games() turns it into completed-game rows. Kept raw
    so main() can also see what's still left to play this week (see
    pending_games_in_latest_week()) from the same single call."""
    return cfbd_get("/games", api_key, year=year, seasonType="both", classification="fbs")


def fetch_games(year, api_key):
    return parse_games(fetch_raw_games(year, api_key), year)


def parse_games(raw, year, through_week=None):
    games = []
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
            # CFBD's own flag -- counts toward conference standings (see
            # conference_leaders()). Verified to reproduce CFBD's /records
            # conference W-L exactly for every team.
            "conference_game": bool(g.get("conferenceGame")),
        })
    fold_title_games_into_regular(games)
    if through_week is not None:
        # After the fold above, so a refiled title game is filtered by its
        # regular-season week, not its postseason one.
        games = [r for r in games if (r["week"] or 0) <= through_week]
    # Sort deterministically -- the CFBD API doesn't guarantee stable ordering
    # between calls for games sharing a timestamp, which would otherwise cause
    # a spurious diff/commit every run even when no game data actually changed.
    games.sort(key=lambda r: (r["date"] or "", r["winner"], r["loser"]))
    return games


def fold_title_games_into_regular(game_rows):
    """Refiles, in place, conference championship games that CFBD tags as
    postseason back into the regular season. CFBD does this for 1998-2000
    (10 games, e.g. Texas A&M's upset of Kansas State in the 1998 Big 12
    title game), which otherwise leaves them out of the "End of Regular
    Season" snapshot -- the one the BCS actually selected from.

    A game is refiled when it's tagged postseason, flagged as a conference
    game, and played (UTC date) on or before the season's last regular-
    season game. Checked against every season 1998-2025: that matches
    exactly those 10 games -- CFBD also flags a few later bowls as
    conference games, but those are always played after the regular
    season ends. A refiled game takes the regular-season week it falls in
    (the first week whose last game is on or after it)."""
    regular = [r for r in game_rows if r["season_type"] == "regular" and r["week"] is not None and r["date"]]
    if not regular:
        return
    last_regular_day = max(r["date"] for r in regular)[:10]
    week_end = {}
    for r in regular:
        week_end[r["week"]] = max(week_end.get(r["week"], ""), r["date"][:10])
    for r in game_rows:
        if r["season_type"] != "postseason" or not r["conference_game"] or not r["date"]:
            continue
        day = r["date"][:10]
        if day > last_regular_day:
            continue
        r["season_type"] = "regular"
        r["week"] = min(wk for wk, end in week_end.items() if end >= day)


def is_army_navy_game(game_row):
    return {game_row["winner"], game_row["loser"]} == {"Army", "Navy"}


def pending_games_in_latest_week(raw_games):
    """Regular-season games still to be played in the latest week that has
    a finished game -- i.e. whether that week is done yet (CFBD files a
    Monday-night game in the same week as the Saturday before it). Raw CFBD
    game dicts, from fetch_raw_games(). Empty once every game that week is
    final, or before any game has been played."""
    regular = [g for g in raw_games if g.get("seasonType") == "regular" and g.get("week") is not None]
    finished_weeks = [g["week"] for g in regular if g.get("completed")]
    if not finished_weeks:
        return []
    week = max(finished_weeks)
    return [g for g in regular if g["week"] == week and not g.get("completed")]


def game_identities(game_rows):
    """One hashable key per completed game, ignoring the score -- for
    telling whether any game has finished since the last snapshot (a later
    CFBD score correction on an already-counted game doesn't count as a
    new one)."""
    return {game_identity(g) for g in game_rows}


def game_identity(g):
    return (g["season"], g["season_type"], g["week"], g["date"], frozenset((g["winner"], g["loser"])))


def new_game_rows(game_rows, previous_rows):
    """The rows in `game_rows` that weren't in `previous_rows` (games.json
    from the last snapshot) -- see game_identities()."""
    seen = game_identities(previous_rows)
    return [g for g in game_rows if game_identity(g) not in seen]


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

# CFBD releases filed under the wrong week, as {(year, cfbd_week):
# corrected_week}. CFBD's 2001 "week 16" BCS Standings is the Dec 9 final
# (Tennessee 6 and LSU 13 -- only true after LSU won the Dec 8 SEC title
# game; Tennessee was 2nd on Dec 3), and the Dec 3 release is missing.
# Read as week 17, it lands on the final snapshot instead of the week
# before it.
KNOWN_RELABELED_RELEASES = {(2001, 16): 17}

# Hand-entered committee releases, best first, as {(year, cfbd_week):
# [team, ...]} -- used in place of whatever CFBD has for that week (after
# KNOWN_RELABELED_RELEASES), keyed by CFBD's week numbering (see
# fetch_committee_rankings()). Team names are CFBD's spellings. Transcribed
# 2026-11-22 from each season's "NCAA Division I FBS/I-A football rankings"
# page on Wikipedia, and checked against CFBD's releases on either side.
MANUAL_RELEASES = {
    # 2001 BCS Standings, Dec 3 -- missing from CFBD. The BCS published 15
    # teams that season.
    (2001, 16): [
        "Miami", "Tennessee", "Nebraska", "Colorado", "Oregon", "Florida", "Texas", "Illinois",
        "Stanford", "Maryland", "Oklahoma", "BYU", "Washington State", "Washington", "South Carolina",
    ],
    # 2011 BCS Standings, Nov 27. CFBD's week 14 is a copy of the Dec 4
    # final, identical in all 25 spots -- it has Houston 19th when Houston
    # was still 12-0 (6th here; it fell to 19th only after losing the C-USA
    # title game on Dec 3).
    (2011, 14): [
        "LSU", "Alabama", "Oklahoma State", "Stanford", "Virginia Tech", "Houston", "Boise State",
        "Arkansas", "Oregon", "Oklahoma", "Kansas State", "South Carolina", "Michigan State",
        "Georgia", "Wisconsin", "Michigan", "Baylor", "TCU", "Nebraska", "Clemson", "Penn State",
        "Texas", "West Virginia", "Southern Miss", "Missouri",
    ],
    # 2019 CFP ranking, Nov 26 -- missing from CFBD.
    (2019, 14): [
        "Ohio State", "LSU", "Clemson", "Georgia", "Alabama", "Utah", "Oklahoma", "Minnesota",
        "Baylor", "Penn State", "Florida", "Wisconsin", "Michigan", "Oregon", "Auburn",
        "Notre Dame", "Iowa", "Memphis", "Cincinnati", "Boise State", "Oklahoma State", "USC",
        "Iowa State", "Virginia Tech", "App State",
    ],
}

# The real BCS title-game matchup per season, as (BCS #1, BCS #2) in the
# final BCS Standings -- the BCS title game was always exactly those two.
# Hand-entered because CFBD's "BCS Standings" are missing or incomplete for
# most BCS seasons (verified 2026-11-22): none at all for 1998-2000 and
# 2002-2006, and 2007-2008 stop a week short of the final release (their
# last release predates conference championship week, so its top 2 --
# Missouri/West Virginia, Alabama/Oklahoma -- were never the matchup).
# Takes precedence over CFBD's standings in real_playoff_field(); 2010-2013
# are listed too and match CFBD's own final standings.
BCS_TITLE_GAMES = {
    1998: ("Tennessee", "Florida State"),
    1999: ("Florida State", "Virginia Tech"),
    2000: ("Oklahoma", "Florida State"),
    2001: ("Miami", "Nebraska"),
    2002: ("Miami", "Ohio State"),
    2003: ("Oklahoma", "LSU"),
    2004: ("USC", "Oklahoma"),
    2005: ("USC", "Texas"),
    2006: ("Ohio State", "Florida"),
    2007: ("Ohio State", "LSU"),
    2008: ("Oklahoma", "Florida"),
    2009: ("Alabama", "Texas"),
    2010: ("Auburn", "Oregon"),
    2011: ("LSU", "Alabama"),
    2012: ("Notre Dame", "Alabama"),
    2013: ("Florida State", "Auburn"),
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
    once the field is picked).

    A committee ranking CFBD files under the postseason is kept as the
    week after the last regular-season release. CFBD does this with 2020's
    final (selection-day) ranking, filing it as "postseason week 1" -- its
    regular "week 16" is the Dec 15 release, from before that season's
    title games."""
    poll_name = "BCS Standings" if year < CFP_ERA_START else "Playoff Committee Rankings"
    raw = cfbd_get("/rankings", api_key, year=year)
    by_week = {}
    postseason = None
    for entry in raw:
        for poll in entry.get("polls", []):
            if poll["poll"] != poll_name:
                continue
            ranks = {r["school"]: r["rank"] for r in poll["ranks"]}
            if entry.get("seasonType") == "postseason":
                postseason = ranks
            elif entry.get("seasonType") == "regular":
                for team in ranks:
                    fix = KNOWN_RANK_FIXES.get((year, entry["week"], team))
                    if fix is not None:
                        ranks[team] = fix
                by_week[KNOWN_RELABELED_RELEASES.get((year, entry["week"]), entry["week"])] = ranks
    if postseason and by_week:
        by_week[max(by_week) + 1] = postseason
    for (manual_year, week), manual_teams in MANUAL_RELEASES.items():
        if manual_year == year:
            by_week[week] = {team: i + 1 for i, team in enumerate(manual_teams)}
    return by_week


def committee_ranks_for_week(rankings_by_week, week):
    """The real committee ranking for an as-of-week-`week` snapshot: week
    (week + 1) in CFBD's own numbering (see fetch_committee_rankings()),
    or None if CFBD has no such release. There's deliberately no fallback
    to week `week` itself: that release is the one the previous week's
    snapshot already shows, so falling back only ever repeats a stale
    ranking (it did for 2019's Nov 24 snapshot, where CFBD is missing the
    Nov 26 release) -- and on a live run it would attach last week's
    ranking to a snapshot made before this week's has aired."""
    if week is None:
        return None
    return rankings_by_week.get(week + 1)


def fetch_cfp_participants(year, api_key):
    """The real CFP field for `year`, via CFBD's dedicated
    /playoffs/cfp/participants endpoint -- {team: seed}. Naturally empty
    before that season's selection actually happens (verified: the
    current not-yet-selected season returns []) and empty for every year
    before the CFP existed (1998-2013 uses the BCS top-2 instead, see
    real_playoff_field())."""
    raw = cfbd_get("/playoffs/cfp/participants", api_key, year=year)
    return {p["team"]["school"]: p["seed"] for p in raw}


def real_playoff_field(year, cfp_participants, final_committee_ranks):
    """The real playoff/BCS field as {team: seed}, or None if that isn't
    determined yet. 2014+: CFP's real field (cfp_participants, empty
    until selection happens -- so this stays None all season until then,
    then flips to the real seeded field). 1998-2013 (no CFP existed): the
    top 2 teams in the final BCS Standings, seeded 1 and 2 by that same
    rank -- the BCS title game was always exactly the top 2, no other
    selection criteria, so rank IS the seed. Taken from BCS_TITLE_GAMES
    (hand-entered, since CFBD's own BCS Standings are incomplete); for a
    BCS season not listed there, from that season's true final ranking
    (`final_committee_ranks`), not just whatever week is current."""
    if year < CFP_ERA_START:
        if year in BCS_TITLE_GAMES:
            return {team: i + 1 for i, team in enumerate(BCS_TITLE_GAMES[year])}
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


def conference_leaders(teams, game_rows, ranked_names, conf_champions=None):
    """{conference: leader} for every conference (independents excluded,
    since there's no conference title to win) -- whoever is first in the
    conference standings as of `game_rows`:

      1. best conference win %,
      2. then more conference wins (3-0 ahead of 1-0),
      3. then head-to-head -- a tied team that beat every other tied team,
      4. then our own rating (`ranked_names`, best-first -- the site's
         order, see rank_by_public_score()).

    A team with no conference games yet sorts below any that has played
    one, so before conference play starts every conference falls through
    to step 4. `conf_champions` ({conference: team}, from
    conference_champions()) overrides all of that once a title game has
    been played -- the champion isn't always the regular-season leader
    (e.g. Clemson beating SMU for the 2024 ACC title).

    One definition shared by the site's "*", the Slack post's "*", and
    every projection's auto bids (via the `conf_leader` flag on each row),
    so they always name the same team."""
    conf_of = {t["school"]: t.get("conference") for t in teams}
    rank_of = {team: i for i, team in enumerate(ranked_names)}
    record = {}
    beat = set()
    for g in game_rows:
        if g.get("season_type") != "regular" or not g.get("conference_game"):
            continue
        record.setdefault(g["winner"], [0, 0])[0] += 1
        record.setdefault(g["loser"], [0, 0])[1] += 1
        beat.add((g["winner"], g["loser"]))

    def standing(team):
        wins, losses = record.get(team, (0, 0))
        return (wins / (wins + losses) if wins + losses else -1.0, wins)

    members = {}
    for team, conf in conf_of.items():
        if conf and conf != "FBS Independents":
            members.setdefault(conf, []).append(team)

    leaders = {}
    for conf, conf_teams in members.items():
        best = max(standing(t) for t in conf_teams)
        tied = sorted(
            (t for t in conf_teams if standing(t) == best),
            key=lambda t: (rank_of.get(t, len(rank_of)), t),
        )
        beat_all = [t for t in tied if all((t, o) in beat for o in tied if o != t)]
        leaders[conf] = beat_all[0] if len(tied) > 1 and beat_all else tied[0]
    leaders.update(conf_champions or {})
    return leaders


def is_group_of_six(conference):
    """Any FBS conference outside the Power Four (independents aren't in
    one -- Notre Dame has its own 2026+ provision)."""
    return bool(conference) and conference not in POWER_FOUR and conference != "FBS Independents"


def conference_leader_field(ranked_teams, season, teams, conf_leaders):
    """The 12-team format's selection rule as a set of teams, shared by
    every playoff-field projection in this module (real_field_projection
    fed committee ranks, playoff_projection fed our own scores).
    `ranked_teams` is [(team, value)] already sorted best-first; `teams`
    gives each team's conference; `conf_leaders` is {conference: leader}
    from conference_leaders().

    2024-2025: the 5 highest-ranked conference leaders + the next 7 best.
    2026+ (P4_AUTO_BID_ERA_START): every Power Four leader regardless of
    rank + the single highest-ranked Group of Six *team* -- "regardless of
    whether it's a conference champion or not" (NCAA.com's automatic-
    qualifier explainer; earlier reports said "highest-ranked G6
    champion") -- plus
    Notre Dame if it's inside the top NOTRE_DAME_TOP_N, with the best
    remaining teams filling the field out to 12. If no Group of Six team
    is in `ranked_teams` at all (possible against the committee's top 25),
    that bid goes at-large.

    2024-2025 auto bids are chosen by each leader's own rank in
    `ranked_teams`. A leader missing from `ranked_teams` altogether (common
    for a Group of Six standings leader against the committee's top 25)
    ranks after every ranked team, team name breaking ties so the result
    never depends on dict order."""
    rank_of = {team: i for i, (team, _) in enumerate(ranked_teams)}

    def by_rank(team):
        return (rank_of.get(team, len(ranked_teams)), team)

    conf_leader = conf_leaders or {}
    if season >= P4_AUTO_BID_ERA_START:
        conf_of = {t["school"]: t.get("conference") for t in teams}
        power_four = [t for c, t in conf_leader.items() if c in POWER_FOUR]
        group_of_six = [team for team, _ in ranked_teams if is_group_of_six(conf_of.get(team))][:1]
        field = set(power_four + group_of_six)
        if rank_of.get("Notre Dame", NOTRE_DAME_TOP_N) < NOTRE_DAME_TOP_N:
            field.add("Notre Dame")
    else:
        field = set(sorted(conf_leader.values(), key=by_rank)[:5])

    at_large = [team for team, _ in ranked_teams if team not in field][:12 - len(field)]
    return field | set(at_large)


def playoff_field_from_ranked(ranked_teams, season, teams=(), conf_leaders=None):
    """The full "who's in" rule for `season`, given `ranked_teams` already
    sorted best-first: top-2 pre-CFP (BCS), top-4 old-CFP, else the 12-team
    conference-leader + at-large field. Shared by real_field_projection
    (fed committee ranks) and playoff_projection (fed our own scores) so
    the era cutoffs and selection rule live in exactly one place."""
    if season < CFP_ERA_START:
        return {team for team, _ in ranked_teams[:2]}
    if season < TWELVE_TEAM_ERA_START:
        return {team for team, _ in ranked_teams[:4]}
    return conference_leader_field(ranked_teams, season, teams, conf_leaders)


def real_field_projection(committee_ranks, season, teams=(), conf_leaders=None):
    """"If the real field were picked today," projected from this week's
    real committee ranking -- the same "conference leaders get automatic
    bids" mechanic as our own playoffFieldProjection (playoff-format.js),
    just fed CFBD's real ranks instead of our own power_score. The leaders
    themselves (`conf_leaders`, from conference_leaders()) are the
    standings leaders -- the best proxy for each eventual champion before
    title games are played -- so this is necessarily a rougher estimate
    than real_playoff_field()'s ground truth, and callers should prefer
    that whenever it's available (see build_snapshots callers). Returns a
    plain set (no seeds -- those only mean something once the real bracket
    is set), or None if there's no ranking to project from yet this week."""
    if not committee_ranks:
        return None
    ranked = sorted(committee_ranks.items(), key=lambda kv: kv[1])
    return playoff_field_from_ranked(ranked, season, teams, conf_leaders)


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


def archive_season_games(archive, season, game_rows, fbs_teams):
    """games_archive.json (see build_games_archive.py) with `season`'s games
    replaced wholesale by `game_rows`' FBS-vs-FBS games -- replaced, not
    appended, so a score correction or a re-run never duplicates a game.
    Sorted the same way as games.json, season first."""
    kept = [r for r in archive if r["season"] != season]
    kept.extend(
        r for r in game_rows
        if r["season"] == season and r["winner"] in fbs_teams and r["loser"] in fbs_teams
    )
    kept.sort(key=lambda r: (r["season"], r["date"] or "", r["winner"], r["loser"]))
    return kept


def load_json(path, default):
    if path.exists():
        with path.open() as f:
            return json.load(f)
    return default


def write_json(path, data):
    """Writes to a temp file and renames it into place, so a process kill
    or crash mid-write can never leave `path` truncated/invalid for the
    next run (or the live site, which fetches these files directly) to
    trip over.

    A list (ratings_history.json, games.json, teams.json) is written one
    compact row per line rather than indent=2: ratings_history.json is the
    site's one big fetch, and this cuts it by ~30% while a weekly update
    still diffs as whole added/changed lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w") as f:
        if isinstance(data, list):
            rows = ",\n".join(json.dumps(row, separators=(",", ":")) for row in data)
            f.write(f"[\n{rows}\n]\n" if data else "[]\n")
        else:
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
    return bool(games_in_week) and all(is_army_navy_game(g) for g in games_in_week)


def army_navy_snapshot_to_replace(history, season, new_games, conf_champions):
    """The as_of of `season`'s latest snapshot when the only games since it
    (`new_games`, see new_game_rows()) are a regular-season Army-Navy played
    after conference championships -- the live run's version of
    build_snapshots()' fold. The caller drops that snapshot and writes the
    new one in its place, dated today, so (as in a backfilled season) the
    "End of Regular Season" snapshot includes Army-Navy and there's no
    second one. None in every other case, including once any bowl has been
    played (then it's an ordinary "Bowl Season" snapshot)."""
    if not conf_champions or not new_games:
        return None
    if not all(g["season_type"] == "regular" and is_army_navy_game(g) for g in new_games):
        return None
    season_rows = [r for r in history if r["season"] == season]
    if not season_rows:
        return None
    latest = max(r["as_of"] for r in season_rows)
    if any(r["as_of"] == latest and r["stage"] != "regular" for r in season_rows):
        return None
    return latest


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


def public_score(value):
    """The 1-decimal power_score exactly as published in
    ratings_history.json. Every ranking on the Python side sorts on this,
    not the unrounded value, so it orders teams the same way the site does
    (published score, then team name): two teams that both read 76.6 are
    tied on the site, and ranking them by hidden extra decimals here would
    let the Slack post and weekly_blurb.txt put a different team in the
    last playoff spot than the site highlights."""
    return round(value, 1)


def rank_by_public_score(results, scores):
    """`results` items, best-first by public_score(), team name breaking
    ties -- the site's own ordering (see public_score())."""
    return sorted(results.items(), key=lambda kv: (-public_score(scores[kv[0]]), kv[0]))


def build_public_rows(
    results, scores, teams, season, as_of, stage="regular",
    committee_ranks=None, playoff_field=None, playoff_seeds=None, conf_leaders=None,
):
    """Like ratings_core.build_snapshot_rows(), but persists power_score
    instead of the raw sos/rating fields -- those never touch a committed
    file. Reuses compute_ratings()'s actual math untouched; this only
    changes what gets written. Rows are compact (see compact_row()): a
    field that would be null or false is left out. `stage` ("regular"/"postseason") lets the
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
    since a projection's "seed" isn't a real fact yet. `conf_leaders`
    ({conference: team}, from conference_leaders()) becomes `conf_leader`
    -- the site can't work out standings or head-to-head itself, so it
    reads this flag for both its "*" and its projection's auto bids. Only
    12-team-era rows (TWELVE_TEAM_ERA_START on) carry it, since only that
    format uses leaders."""
    conf_of = {t["school"]: t.get("conference") for t in teams}
    leader_teams = set((conf_leaders or {}).values()) if season >= TWELVE_TEAM_ERA_START else set()
    rows = []
    for team, r in results.items():
        rows.append(compact_row({
            "season": season,
            "as_of": as_of,
            "stage": stage,
            "team": team,
            "conference": conf_of.get(team),
            "wins": r["wins"],
            "losses": r["losses"],
            "power_score": public_score(scores[team]),
            **committee_row_fields(team, committee_ranks, playoff_field, playoff_seeds),
            "conf_leader": team in leader_teams,
        }))
    return rows


def compact_row(row):
    """`row` without its null and false fields. ratings_history.json is the
    site's one big fetch, and most rows are unranked, outside the playoff
    field, and not a conference leader -- every page already reads a
    missing field the same as null/false. (0 and 0.0 are kept: a winless
    record or a bottom-of-the-scale score is real data.)"""
    return {k: v for k, v in row.items() if v is not None and v is not False}


def committee_row_fields(team, committee_ranks, playoff_field, playoff_seeds):
    """A row's committee_rank/made_playoff/seed -- see build_public_rows()
    for what each means. Shared with update_cfp_ranks(), which rewrites
    just these three on an existing snapshot."""
    return {
        "committee_rank": (committee_ranks or {}).get(team),
        "made_playoff": (team in playoff_field) if playoff_field is not None else None,
        "seed": (playoff_seeds or {}).get(team),
    }


def fetch_committee_fields(year, api_key, current_week, teams, conf_leaders):
    """(committee_ranks, playoff_field, playoff_seeds) for a live snapshot
    as of `current_week` -- 2 CFBD calls. The field is the real one once
    it's been selected, else a same-week projection from the committee
    ranking (see build_public_rows())."""
    print("Fetching committee rankings...")
    rankings_by_week = fetch_committee_rankings(year, api_key)
    committee_ranks = committee_ranks_for_week(rankings_by_week, current_week)
    print(f"  {len(committee_ranks) if committee_ranks else 0} teams ranked this week")

    print("Fetching real playoff field...")
    cfp_participants = fetch_cfp_participants(year, api_key)
    if year >= CFP_ERA_START:
        exact_seeds = real_playoff_field(year, cfp_participants, committee_ranks)
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
        print(f"  {len(exact_seeds)} teams (real field determined)")
        return committee_ranks, set(exact_seeds.keys()), exact_seeds
    playoff_field = real_field_projection(committee_ranks, year, teams, conf_leaders)
    print(f"  {len(playoff_field) if playoff_field else 0} teams (projected -- real field not determined yet)")
    return committee_ranks, playoff_field, None


def update_cfp_ranks(year, api_key, out_dir):
    """The Wednesday "CFP ranks" run: refreshes committee_rank/made_playoff/
    seed on the season's latest snapshot in place, once the committee's
    Tuesday-night ranking is out -- without recomputing ratings, adding a
    new snapshot, or posting to Slack. Reads the teams.json/games.json the
    weekly run wrote alongside that snapshot, so the ranking is looked up
    for exactly the week those ratings cover (not whatever has been played
    since, like a Tuesday-night MAC game from the next week)."""
    history_path = out_dir / "ratings_history.json"
    history = load_json(history_path, [])
    teams = load_json(out_dir / "teams.json", [])
    game_rows = load_json(out_dir / "games.json", [])
    season_as_ofs = [r["as_of"] for r in history if r["season"] == year]
    if not season_as_ofs:
        print(f"No {year} snapshot yet -- nothing to add CFP ranks to.")
        return
    if not game_rows or any(g["season"] != year for g in game_rows):
        sys.exit(f"games.json isn't {year}'s -- run the weekly update for {year} first.")

    latest = max(season_as_ofs)
    # games.json and the latest snapshot are normally written by the same
    # weekly run, but a manual --as-of/--week run can break that pairing --
    # confirm every team's record matches before writing, rather than
    # silently attaching one week's ranking to another week's snapshot.
    records = compute_ratings(teams, to_rating_games(game_rows))
    mismatched = [
        r["team"] for r in history
        if r["season"] == year and r["as_of"] == latest
        and (r["wins"], r["losses"]) != (records.get(r["team"], {}).get("wins"), records.get(r["team"], {}).get("losses"))
    ]
    if mismatched:
        sys.exit(
            f"games.json doesn't match the {latest} snapshot ({len(mismatched)} teams' records differ, "
            f"e.g. {mismatched[0]}) -- re-run the weekly update first."
        )
    current_week = current_week_number(game_rows)
    # The leaders the weekly run already worked out for this snapshot --
    # reused rather than recomputed, so the projection's auto bids can't
    # drift from the "*" the site is already showing.
    conf_leaders = {
        r.get("conference"): r["team"] for r in history
        if r["season"] == year and r["as_of"] == latest and r.get("conf_leader")
    }
    committee_ranks, playoff_field, playoff_seeds = fetch_committee_fields(
        year, api_key, current_week, teams, conf_leaders,
    )
    if committee_ranks is None and playoff_seeds is None:
        # CFBD doesn't have this week's ranking yet (or there isn't one) --
        # leave the snapshot as it is; the 17:23 backup run tries again.
        print(f"No committee ranking for the {latest} snapshot (week {current_week}) yet -- nothing written.")
        return
    updated = 0
    for i, r in enumerate(history):
        if r["season"] == year and r["as_of"] == latest:
            fields = committee_row_fields(r["team"], committee_ranks, playoff_field, playoff_seeds)
            new_row = compact_row({**r, **fields})
            updated += new_row != r
            history[i] = new_row
    write_json(history_path, history)
    print(f"Updated CFP ranks on the {latest} snapshot (week {current_week}): {updated} rows changed")


def playoff_projection(scores, season, teams=(), conf_leaders=None):
    """Mirrors the site's own projection (playoffFieldProjection in
    playoff-format.js): a running "if it ended today" field from that
    snapshot's own scores, not the actual selection. 1998-2013 (BCS): top
    2. 2014-2023 (old CFP): top 4. 2024+: the 12-team conference-leader +
    at-large field -- see conference_leader_field() for 2024-2025's vs.
    2026+'s rules. (Currently only ever called for season >= 2024 -- see
    format_weekly_blurb's own top-10 branch for the pre-2024 blurb -- but
    kept correct for every era rather than leaving dead-but-wrong code.)"""
    # Ranked on the published (rounded) score with team name as the
    # tie-break -- the site's own ordering, see public_score(). Ties are
    # common at 1 decimal (e.g. #12 and #13 in 8 of the first 252
    # snapshots), and ranking on the unrounded value instead would let this
    # pick a different team for the last slot than the site does. Passing
    # `scores` as both arguments gives (team, score) pairs.
    ranked = rank_by_public_score(scores, scores)
    return playoff_field_from_ranked(ranked, season, teams, conf_leaders)


def format_weekly_blurb(results, scores, season, teams=(), title=None, conf_leaders=None):
    # Rank by the same (possibly blended) scores being displayed -- sorting
    # by raw rating instead would let the blend reorder teams without the
    # printed rank agreeing with the printed number -- and on the published
    # rounded value, so ranks match the site's (see public_score()).
    ranked = rank_by_public_score(results, scores)
    if season < TWELVE_TEAM_ERA_START:
        # What actually got posted pre-2024: top 10, not a full top 25.
        selected = {team for team, _ in ranked[:10]}
    else:
        # From 2024 on, just the projected playoff field -- true rank
        # number kept, so an auto-bid conference champ ranked outside the
        # top 12 overall still shows its real position (e.g. 1-10, 14, 16).
        selected = playoff_projection(scores, season, teams, conf_leaders)
    lines = [title or "Power Ratings"]
    for i, (team, r) in enumerate(ranked, start=1):
        if team not in selected:
            continue
        lines.append(f"{i}. {team} ({r['wins']}-{r['losses']})")
    return "\n".join(lines)


def format_slack_blurb(results, scores, season, week_label, teams=(), conf_leaders=None, link=None):
    """The Slack-posted version of the blurb -- 12-team-era only (we'll
    never post this for a past pre-2024 season, so there's no top-10/top-4/
    top-2 branching here like format_weekly_blurb has). Uses Slack's
    mrkdwn syntax (*bold*, _italic_), not plain text or real Markdown --
    this is only ever meant to go straight into a Slack message, unlike
    format_weekly_blurb's output. Title is "Week N" (or "End of Regular
    Season"/"Bowl Season"/"End of Bowl Season" -- see season_week_label())
    instead of a date, since a Slack reader cares what week this is, not
    which calendar day it happened to run. Team lines get a "*" marking
    each conference's current leader -- the same teams, and the same
    marker, as the site's standings table (whose CFP column uses "†"
    instead); the footnote explaining
    that and the closing link are italicized so they read as asides rather
    than part of the ranking itself."""
    ranked = rank_by_public_score(results, scores)
    selected = playoff_projection(scores, season, teams, conf_leaders)
    leaders = set((conf_leaders or {}).values())

    lines = [f"*Belmore Rankings {season} - {week_label}*"]
    for i, (team, r) in enumerate(ranked, start=1):
        if team not in selected:
            continue
        marker = " *" if team in leaders else ""
        lines.append(f"{i}. {team} ({r['wins']}-{r['losses']}){marker}")
    lines.append("")
    lines.append("_* = conference leader (or champion, once the title game is played)_")
    if link:
        # Wrapped in Slack's <url> link syntax, not left as a bare URL --
        # Slack's auto-linker treats "_" as a valid URL character, so a
        # bare URL immediately followed by the closing "_" for italics
        # swallows it into the link (breaking the link and leaving the
        # italics unclosed). The explicit <...> unambiguously ends the URL
        # at the ">", so the closing "_" lands outside it.
        lines.append(f"_Full rankings available at <{link}>_")
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
    parser.add_argument(
        "--slack-message-out", default=None,
        help="Write the Slack post's text to this file (2024+ seasons only) "
             "instead of posting it -- the workflow posts it only after the "
             "data commit is pushed.",
    )
    parser.add_argument(
        "--skip-if-no-new-games", action="store_true",
        help="Exit without writing anything if no game has finished since the "
             "last snapshot (compared against games.json) -- makes repeat "
             "scheduled runs in the same week no-ops.",
    )
    parser.add_argument(
        "--require-complete-week", action="store_true",
        help="Exit without writing anything if the current regular-season week "
             "still has games to play (e.g. a Monday-night game).",
    )
    parser.add_argument(
        "--cfp-ranks-only", action="store_true",
        help="Only refresh the committee ranks on the latest snapshot (see "
             "update_cfp_ranks()) -- no ratings, no new snapshot, no Slack.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("CFBD_API_KEY")
    if not api_key:
        sys.exit("CFBD_API_KEY environment variable is not set.")

    as_of = args.as_of or datetime.now(timezone.utc).date().isoformat()
    out_dir = Path(args.out_dir)

    if args.cfp_ranks_only:
        update_cfp_ranks(args.year, api_key, out_dir)
        return

    print(f"Fetching {args.year} games...")
    raw_games = fetch_raw_games(args.year, api_key)
    game_rows = parse_games(raw_games, args.year, through_week=args.week)
    print(f"  {len(game_rows)} completed games")

    # Both checks run before anything is written, so a skipped run leaves
    # nothing to commit and no Slack message for the workflow to post.
    new_games = new_game_rows(game_rows, load_json(out_dir / "games.json", []))
    if args.skip_if_no_new_games and not new_games:
        print("No games have finished since the last snapshot -- skipping.")
        return
    # Regular season only: bowl season is a single CFBD "week" spread over
    # a month, so waiting for it to finish would mean no update until the
    # national championship.
    if args.require_complete_week and current_stage(game_rows) == "regular":
        pending = pending_games_in_latest_week(raw_games)
        if pending:
            print(f"Week {pending[0]['week']} isn't finished -- {len(pending)} game(s) still to play:")
            for g in pending:
                print(f"  {g['awayTeam']} at {g['homeTeam']} ({g.get('startDate')})")
            print("Skipping -- a later run will pick it up once they're final.")
            return

    print(f"Fetching {args.year} FBS teams...")
    teams = fetch_teams(args.year, api_key)
    print(f"  {len(teams)} teams")
    if not teams:
        sys.exit(f"No FBS teams returned for year {args.year} -- check --year and try again.")

    write_json(out_dir / "teams.json", teams)
    write_json(out_dir / "games.json", game_rows)
    # The rivalry page's all-seasons game record -- games.json is replaced
    # at every season rollover, so this season's games are copied here too.
    # Not for a --week run, whose game_rows stop partway through the season
    # and would cut the archive's copy of it short.
    archive_path = out_dir / "games_archive.json"
    if args.week is None:
        write_json(archive_path, archive_season_games(
            load_json(archive_path, []), args.year, game_rows, {t["school"] for t in teams},
        ))

    history_path = out_dir / "ratings_history.json"
    history = load_json(history_path, [])

    print("Computing ratings...")
    results = compute_ratings(teams, to_rating_games(game_rows))
    current_week = current_week_number(game_rows)
    # Last season's ratings only matter while the preseason blend is on
    # (weeks 1-6) -- skipping them after that saves 2 CFBD calls a run.
    prior_raw = (
        prior_season_raw_ratings(history, args.year, api_key)
        if preseason_blend_weight(current_week) > 0 else None
    )
    blended_raw = blend_raw_ratings(results, prior_raw, current_week)
    scores = normalize_values(blended_raw)
    stage = current_stage(game_rows)
    conf_champions = conference_champions(game_rows, teams)
    conf_leaders = conference_leaders(
        teams, game_rows, [team for team, _ in rank_by_public_score(results, scores)], conf_champions,
    )

    # Army-Navy, the week after conference championships, replaces that
    # week's snapshot instead of adding one -- the same fold
    # backfill_history.build_snapshots() makes -- and gets no Slack post,
    # which would otherwise repeat the "End of Regular Season" one.
    replaced_as_of = None if args.as_of else army_navy_snapshot_to_replace(
        history, args.year, new_games, conf_champions,
    )
    if replaced_as_of:
        print(f"Only Army-Navy since the {replaced_as_of} snapshot -- replacing it rather than adding one.")
        history = [r for r in history if (r["season"], r["as_of"]) != (args.year, replaced_as_of)]

    # Regular-season runs go out Sunday-Tuesday morning, before that week's
    # ranking airs Tuesday night, so this normally finds none -- the
    # Wednesday --cfp-ranks-only run fills it in.
    committee_ranks, playoff_field, playoff_seeds = fetch_committee_fields(
        args.year, api_key, current_week, teams, conf_leaders,
    )

    new_rows = build_public_rows(
        results, scores, teams, season=args.year, as_of=as_of, stage=stage,
        committee_ranks=committee_ranks, playoff_field=playoff_field, playoff_seeds=playoff_seeds,
        conf_leaders=conf_leaders,
    )

    # Colors almost never change, so /teams (every team CFBD has ever
    # tracked) is only fetched when a current FBS team has none yet --
    # e.g. a team new to FBS.
    colors_path = out_dir / "team_colors.json"
    colors = load_json(colors_path, {})
    if {t["school"] for t in teams} - set(colors):
        print("Fetching team colors...")
        all_team_names = {t["school"] for t in teams} | {r["team"] for r in history}
        colors = fetch_team_colors(api_key, all_team_names)
        write_json(colors_path, colors)
        print(f"  {len(colors)} teams with colors")

    history = merge_history_rows(history, new_rows)
    write_json(history_path, history)

    blurb = format_weekly_blurb(
        results, scores, args.year, teams, title=f"CFB Power Ratings -- {args.year} as of {as_of}",
        conf_leaders=conf_leaders,
    )
    (out_dir / "weekly_blurb.txt").write_text(blurb + "\n")

    # Slack posting only makes sense for the 12-team era -- we'll never
    # run this script against a past pre-2024 season, so there's no need
    # for format_slack_blurb to handle the older top-10/top-4/top-2 eras.
    # Only written here, never posted: the workflow posts it as its own
    # step after the data commit is pushed, so a Slack failure can't block
    # the site's data update, and a push that fails can't leave a Slack
    # post behind for the backup cron run to repeat.
    if args.slack_message_out and args.year >= TWELVE_TEAM_ERA_START and not replaced_as_of:
        week_label = season_week_label(current_week, stage, conf_champions, game_rows)
        slack_text = format_slack_blurb(
            results, scores, args.year, week_label, teams, conf_leaders=conf_leaders, link=SITE_URL,
        )
        Path(args.slack_message_out).write_text(slack_text)
        print(f"Wrote Slack message to {args.slack_message_out}")

    top = rank_by_public_score(results, scores)[:5]
    print(f"\nTop 5 as of {as_of}:")
    for i, (team, r) in enumerate(top, start=1):
        print(f"  {i}. {team} ({r['wins']}-{r['losses']}) -- {scores[team]:.1f}")

    print(
        f"\nWrote {out_dir}/teams.json, {out_dir}/games.json, {out_dir}/games_archive.json, "
        f"{out_dir}/ratings_history.json, {out_dir}/weekly_blurb.txt (and team_colors.json if fetched)"
    )


if __name__ == "__main__":
    main()
