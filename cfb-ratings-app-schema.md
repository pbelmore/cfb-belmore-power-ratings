# CFB Power Ratings App — Scoping Doc

A separate project from the locks parlay app. Where that one needs several
people writing picks every week, this one has a single data pipeline (you)
and a group of viewers (your friends) — a simpler shape, closer to "a
personal dashboard that auto-updates" than "a shared tool people edit."

## What's already validated (attached: `ratings_core.py`)

The rating math itself doesn't need to be invented — it's the same
`Rating = SOS x Win%^2` formula from your Excel workbook, just ported to
plain Python functions instead of spreadsheet formulas. I tested it against
the full real 2025 season (933 games, 136 teams) and it reproduces the
Excel workbook's numbers exactly. Three functions:

- `compute_ratings(teams, games)` — the core calc. Same two-pass logic as
  the spreadsheet: win% first, then SOS (which needs everyone's win% to
  already exist, since it's built from opponents' rates).
- `build_snapshot_rows(...)` — turns one run's results into a row-per-team
  record, ready to append to a history file. This is the piece that makes
  week-over-week and year-over-year trend charts trivial later: they're
  just filters/group-bys on this one growing table, not a new tab per week.
- `format_weekly_blurb(...)` — a plain-text ranked list, ready to paste
  into your group chat. (Sample output using the real, final 2025 numbers:
  Indiana at #1, 15-0, 0.5415 — matches what the workbook says.)

This module is the reusable core; everything below is what wraps around it.

## Data model

Long format instead of wide spreadsheet columns — one row per fact, not one
column per week:

- **`teams`** — current-season roster: school, conference. Refreshed from
  CFBD each run, same as `pull_cfb_schedule.py` already does.
- **`games`** — one row per completed game: season, week, date, winner,
  winner_pts, loser, loser_pts, location (home/away/neutral), notes.
  Same shape as the Schedule tab.
- **`ratings_history`** — one row per team per run: season, as_of (date),
  team, conference, wins, losses, win_pct, sos, rating. This is the table
  `build_snapshot_rows()` produces. Everything the app displays — current
  standings, a team's trend line, this year vs. last year — reads from
  this one table.

Storage: given the scale here (136 teams x ~20 runs/season x a handful of
seasons — a few thousand rows a year), a database server is overkill.
Plain JSON or CSV files committed to the repo work fine, are easy to
inspect by eye, and get a free audit trail from git history itself. SQLite
is a reasonable upgrade later if querying gets awkward, but I wouldn't
start there.

## The weekly pipeline (GitHub Actions)

GitHub Actions can run a script on a schedule for free — this is what
makes "auto pull results every week" mean *actually* hands-off, not just
"a script I still have to remember to run." Rough shape (to refine with
Claude Code once the repo exists):

```yaml
# .github/workflows/weekly-update.yml
on:
  schedule:
    - cron: "0 13 * * 2"   # Tuesdays, 9am ET
  workflow_dispatch:        # lets you also trigger it manually, e.g. after bowls
jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pip install requests
      - run: python scripts/weekly_update.py --year 2026
        env:
          CFBD_API_KEY: ${{ secrets.CFBD_API_KEY }}
      - run: |
          git config user.name "ratings-bot"
          git commit -am "Weekly update $(date +%F)" && git push
```

`weekly_update.py` would be `pull_cfb_schedule.py`'s data-pulling logic,
minus the Excel-writing part, plus a call to `compute_ratings()` and
`build_snapshot_rows()` to append this week's rows to `ratings_history`.

Your `CFBD_API_KEY` goes in the repo's encrypted Secrets, not committed as
plaintext — Claude Code can walk you through adding it when you set the
repo up.

## The two outputs you asked for

**A real website** — GitHub Pages hosts a static site for free straight
out of the repo. A simple page reading `ratings_history` can show the
current week's table plus a couple of trend charts (a team's rating across
the season, a conference's average over time, this year vs. last). No
login, no server to maintain — your friends just get a link.

**A pasteable weekly blurb** — `format_weekly_blurb()` already produces
this. The Action can write it to a file each run (or, if you want it fully
automatic, post it directly to a Discord/Slack webhook or similar) so
it's sitting there ready to copy into the group chat, same as you do
manually today.

Both read from the same `ratings_history` data — no duplicate logic.

## Suggested build order

1. New private repo (public is fine too, nothing sensitive in it besides
   the API key, which stays in Secrets either way).
2. Add `ratings_core.py` (attached, already validated) and a
   `weekly_update.py` that wires it to the CFBD pull, writing
   `teams.json` / `games.json` / `ratings_history.json`.
3. Get the GitHub Action running on a manual trigger first, confirm a
   week's data lands correctly, *then* turn on the schedule.
4. Bare-bones site: literally just render the current week's table from
   `ratings_history.json`. Get that live before worrying about charts.
5. Add the trend charts and the auto-posted blurb once the basics are
   solid.

Steps 2 onward are exactly the kind of thing to hand to Claude Code
directly — describe each step in plain English against this doc, and it
makes the actual commits.

## Open decisions for when you create the repo

- Repo name and public/private (private costs nothing on GitHub, if
  you'd rather keep it out of public view).
- Text blurb, rendered image, or both, for the group-chat output.
- Auto-post to wherever your group chat lives (webhook), or keep it as
  "the text is ready, you paste it" — the latter is less setup and keeps
  you in control of *when* it sends.
