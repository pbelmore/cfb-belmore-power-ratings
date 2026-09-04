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
// with `.team` and `.conference`. Returns an ordered array (position 0 =
// #1 seed) of the projected field for `season`.
function playoffFieldProjection(rankedTeams, season) {
  if (season < TWELVE_TEAM_ERA_START) {
    const fieldSize = season < CFP_ERA_START ? 2 : 4;
    return rankedTeams.slice(0, fieldSize).map(r => r.team);
  }
  // Each conference's entry is set on the first (i.e. best) team from that
  // conference encountered while scanning rankedTeams best-first, so the
  // Map's insertion order is already the conferences' leaders sorted
  // best-first -- no separate re-sort needed before taking the top 5.
  // Independents are excluded from auto bids since there's no conference
  // championship to win one.
  const leaderByConf = new Map();
  for (const r of rankedTeams) {
    if (!r.conference || r.conference === 'FBS Independents') continue;
    if (!leaderByConf.has(r.conference)) leaderByConf.set(r.conference, r.team);
  }
  const leaderNames = new Set([...leaderByConf.values()].slice(0, 5));
  const atLarge = rankedTeams.filter(r => !leaderNames.has(r.team)).slice(0, 7).map(r => r.team);
  return [...leaderNames, ...atLarge];
}
