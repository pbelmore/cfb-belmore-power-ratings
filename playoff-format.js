// Shared playoff-field-selection rule -- used by index.html's live standings
// (getPlayoffProjection) and accuracy.html's accuracy dashboard
// (orderedProjection), so both pages always agree on what "the projected
// field" means for a given season/week. Mirrors the equivalent Python logic
// in scripts/weekly_update.py (playoff_field_from_ranked/conference_leader_field).

// Playoff-format era boundaries -- the one place these cutoffs live on the
// JS side (see the same-named constants in weekly_update.py for the Python
// side). P4_AUTO_BID_ERA_START: from 2026, all four Power Four champions get
// auto bids regardless of rank (plus the single highest-ranked Group of Six
// champion), replacing 2024-2025's "five highest-ranked conference
// champions", and Notre Dame is guaranteed a bid if it's in the top 12.
const CFP_ERA_START = 2014;
const TWELVE_TEAM_ERA_START = 2024;
const P4_AUTO_BID_ERA_START = 2026;
const POWER_FOUR = new Set(['ACC', 'Big Ten', 'Big 12', 'SEC']);
const NOTRE_DAME_TOP_N = 12;

// Map of conference -> its current leader: the best-ranked/-scored team in
// that conference (rankedTeams is row-like objects with `.team` and
// `.conference`, already sorted best-first), independents excluded since
// there's no conference championship to win. confChampions (optional, a Map
// of conference -> team) is the actual championship-game winner for any
// conference whose title game has already been played this snapshot (see
// `conf_champion` on each row, and conference_champions() in
// weekly_update.py) -- it overrides the rank-based stand-in, since the two
// aren't always the same team (e.g. Clemson beating a higher-ranked SMU
// for the 2024 ACC title).
function conferenceLeaders(rankedTeams, confChampions) {
  const ranked = new Set(rankedTeams.map(r => r.team));
  const confLeader = new Map();
  for (const r of rankedTeams) {
    if (!r.conference || r.conference === 'FBS Independents') continue;
    if (!confLeader.has(r.conference)) confLeader.set(r.conference, r.team);
  }
  for (const [conf, champ] of confChampions || []) {
    if (confLeader.has(conf) && ranked.has(champ)) confLeader.set(conf, champ);
  }
  return confLeader;
}

// Returns an ordered array of the projected field for `season`: the
// auto-bid conference leaders first, then the at-large teams, each
// best-first. 2024-2025: the 5 highest-ranked conference leaders + the next
// 7 best. 2026+: every Power Four leader regardless of rank + the single
// highest-ranked Group of Six leader, plus Notre Dame if it's inside the
// top 12 (listed with the at-large teams -- a guaranteed bid, not a
// conference leader), with the best remaining teams filling out 12.
function playoffFieldProjection(rankedTeams, season, confChampions) {
  if (season < TWELVE_TEAM_ERA_START) {
    const fieldSize = season < CFP_ERA_START ? 2 : 4;
    return rankedTeams.slice(0, fieldSize).map(r => r.team);
  }
  const rankOf = new Map(rankedTeams.map((r, i) => [r.team, i]));
  const byRank = (a, b) => rankOf.get(a) - rankOf.get(b);
  const confLeader = conferenceLeaders(rankedTeams, confChampions);

  // Auto bids go by each leader's own rank, not by conference-discovery
  // order -- those can differ once a real champion replaces a
  // higher-ranked stand-in from the same conference.
  let leaders, guaranteed = [], atLargeSlots = 7;
  if (season >= P4_AUTO_BID_ERA_START) {
    const entries = [...confLeader];
    const powerFour = entries.filter(([c]) => POWER_FOUR.has(c)).map(([, t]) => t);
    const groupOfSix = entries.filter(([c]) => !POWER_FOUR.has(c)).map(([, t]) => t).sort(byRank).slice(0, 1);
    leaders = [...powerFour, ...groupOfSix].sort(byRank);
    if (rankOf.has('Notre Dame') && rankOf.get('Notre Dame') < NOTRE_DAME_TOP_N) guaranteed = ['Notre Dame'];
    atLargeSlots = 12 - leaders.length - guaranteed.length;
  } else {
    leaders = [...confLeader.values()].sort(byRank).slice(0, 5);
  }

  const taken = new Set([...leaders, ...guaranteed]);
  const atLarge = [
    ...guaranteed,
    ...rankedTeams.filter(r => !taken.has(r.team)).slice(0, atLargeSlots).map(r => r.team),
  ].sort(byRank);
  return [...leaders, ...atLarge];
}
