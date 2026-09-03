"""
ratings_core.py -- pure-Python port of the Rating = SOS x Win%^2 formula
from the Excel workbook. No spreadsheet involved: this operates directly on
plain team/game data structures, so it's the piece a real app's weekly job
would call.

compute_ratings() is intentionally the same two-pass logic as the Excel
rebuild: win% first (needs nothing but each team's own W/L), then SOS
(needs every team's win% already computed, since it's built from
opponents' win rates).
"""

from dataclasses import dataclass, field


@dataclass
class Game:
    winner: str
    winner_pts: int
    loser: str
    loser_pts: int
    location: str  # "" (winner home), "@" (winner away), "N" (neutral)


def compute_ratings(teams, games):
    """
    teams: list of team names (or {school, conference} dicts -- name only matters here)
    games: list of Game
    returns: {team_name: {"wins": int, "losses": int, "win_pct": float,
                           "sos": float, "rating": float}}
    """
    names = {t["school"] if isinstance(t, dict) else t for t in teams}

    wins = {n: 0 for n in names}
    losses = {n: 0 for n in names}
    for g in games:
        if g.winner in wins:
            wins[g.winner] += 1
        if g.loser in losses:
            losses[g.loser] += 1

    win_pct = {}
    for n in names:
        w, l = wins[n], losses[n]
        win_pct[n] = w / (w + l) if (w + l) else 0.0

    def loc_weight_for_winner(loc):
        if loc == "N":
            return 1.0
        return 1.05 if loc == "@" else 0.95  # winner away -> 1.05, winner home -> 0.95

    def loc_weight_for_loser(loc):
        if loc == "N":
            return 1.0
        return 0.95 if loc == "@" else 1.05  # winner away -> loser home -> 0.95

    sos_sum = {n: 0.0 for n in names}
    for g in games:
        w_pct_loser = win_pct.get(g.loser, 0.0)
        w_pct_winner = win_pct.get(g.winner, 0.0)
        if g.winner in sos_sum:
            sos_sum[g.winner] += w_pct_loser * loc_weight_for_winner(g.location)
        if g.loser in sos_sum:
            sos_sum[g.loser] += w_pct_winner * loc_weight_for_loser(g.location)

    results = {}
    for n in names:
        w, l = wins[n], losses[n]
        games_played = w + l
        sos = sos_sum[n] / games_played if games_played else 0.0
        rating = sos * (win_pct[n] ** 2)
        results[n] = {
            "wins": w, "losses": l,
            "win_pct": round(win_pct[n], 6),
            "sos": round(sos, 6),
            "rating": round(rating, 6),
        }
    return results


def build_snapshot_rows(results, teams, season, as_of):
    """One row per team for this week's run -- this is what gets appended to
    the long-format history file. `as_of` is any label you want to sort/filter
    on later (a date string works well, e.g. "2026-09-16")."""
    conf_of = {
        (t["school"] if isinstance(t, dict) else t): (t.get("conference") if isinstance(t, dict) else None)
        for t in teams
    }
    rows = []
    for team, r in results.items():
        rows.append({
            "season": season,
            "as_of": as_of,
            "team": team,
            "conference": conf_of.get(team),
            **r,
        })
    return rows


def format_weekly_blurb(results, top_n=25, title=None):
    """Plain-text ranking, ready to paste into a group chat."""
    ranked = sorted(results.items(), key=lambda kv: kv[1]["rating"], reverse=True)[:top_n]
    lines = [title or "Power Ratings"]
    for i, (team, r) in enumerate(ranked, start=1):
        lines.append(f"{i}. {team} ({r['wins']}-{r['losses']}) -- {r['rating']:.4f}")
    return "\n".join(lines)
