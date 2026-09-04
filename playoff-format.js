// Shared playoff-field-selection rule -- used by index.html's live standings
// (getPlayoffProjection) and accuracy.html's accuracy dashboard
// (orderedProjection), so both pages always agree on what "the projected
// field" means for a given season/week. Mirrors the equivalent Python logic
// in scripts/weekly_update.py (playoff_field_from_ranked/conference_leader_field).

// Playoff-format era boundaries -- the one place these two cutoffs live on
// the JS side (see CFP_ERA_START/TWELVE_TEAM_ERA_START in weekly_update.py
// for the Python side of the same two constants).
const CFP_ERA_START = 2014;
const TWELVE_TEAM_ERA_START = 2024;

// rankedTeams: array of row-like objects already sorted best-first, each
// with `.team` and `.conference`. confChampions (optional, a Map of
// conference -> team) is the actual championship-game winner for any
// conference whose title game has already been played this snapshot (see
// `conf_champion` on each row, and conference_champions() in
// weekly_update.py) -- it overrides the "best-ranked/-scored team in that
// conference" stand-in used for a conference whose title game hasn't been
// played yet, since the two aren't always the same team (e.g. Clemson
// beating a higher-ranked SMU for the 2024 ACC title, then correctly
// holding the ACC's auto bid over SMU). Returns an ordered array (position
// 0 = #1 seed) of the projected field for `season`.
function playoffFieldProjection(rankedTeams, season, confChampions) {
  if (season < TWELVE_TEAM_ERA_START) {
    const fieldSize = season < CFP_ERA_START ? 2 : 4;
    return rankedTeams.slice(0, fieldSize).map(r => r.team);
  }
  confChampions = confChampions || new Map();
  const rankOf = new Map(rankedTeams.map((r, i) => [r.team, i]));

  // Each conference's entry is set on the first (i.e. best) team from that
  // conference encountered while scanning rankedTeams best-first --
  // independents are excluded from auto bids since there's no conference
  // championship to win one -- then swapped for the real champion where
  // confChampions knows one.
  const confLeader = new Map();
  for (const r of rankedTeams) {
    if (!r.conference || r.conference === 'FBS Independents') continue;
    if (!confLeader.has(r.conference)) confLeader.set(r.conference, r.team);
  }
  for (const [conf, champ] of confChampions) {
    if (confLeader.has(conf) && rankOf.has(champ)) confLeader.set(conf, champ);
  }

  // Ordered by each leader's own rank, not by conference-discovery order --
  // those can differ once a real champion replaces a higher-ranked
  // stand-in from the same conference.
  const orderedLeaders = [...confLeader.values()].sort((a, b) => rankOf.get(a) - rankOf.get(b));
  const leaderNames = new Set(orderedLeaders.slice(0, 5));
  const atLarge = rankedTeams.filter(r => !leaderNames.has(r.team)).slice(0, 7).map(r => r.team);
  return [...leaderNames, ...atLarge];
}
