// Shared playoff-field-selection rule -- used by index.html's live standings
// and accuracy.html's accuracy dashboard, so both pages always agree on what
// "the projected field" means for a given season/week. Mirrors the
// equivalent Python logic in scripts/weekly_update.py
// (playoff_field_from_ranked/conference_leader_field).

// Playoff-format era boundaries -- the one place these cutoffs live on the
// JS side (see the same-named constants in weekly_update.py for the Python
// side). P4_AUTO_BID_ERA_START: from 2026, all four Power Four champions get
// auto bids regardless of rank (plus the single highest-ranked Group of Six
// champion), replacing 2024-2025's "five highest-ranked conference
// champions", and Notre Dame is guaranteed a bid if it's in the top 12.
// STRAIGHT_SEEDING_START: JS-only (Python never seeds) -- 2024 gave the four
// best-ranked conference champions seeds 1-4 and seeded the rest by rank;
// from 2025 the whole field is seeded straight by rank.
const CFP_ERA_START = 2014;
const TWELVE_TEAM_ERA_START = 2024;
const STRAIGHT_SEEDING_START = 2025;
const P4_AUTO_BID_ERA_START = 2026;
const POWER_FOUR = new Set(['ACC', 'Big Ten', 'Big 12', 'SEC']);
const NOTRE_DAME_TOP_N = 12;

// 2 (BCS title game), 4 (old CFP), or 12.
function fieldSizeFor(season) {
  return season < CFP_ERA_START ? 2 : season < TWELVE_TEAM_ERA_START ? 4 : 12;
}

// Map of conference -> actual championship-game winner, from one
// snapshot's rows (`conf_champion` -- see conference_champions() in
// weekly_update.py). Empty until title games are played.
function confChampionsFromRows(rows) {
  return new Map(rows.filter(r => r.conf_champion).map(r => [r.conference, r.team]));
}

// Map of conference -> its current leader: the best-ranked/-scored team in
// that conference (rankedTeams is row-like objects with `.team` and
// `.conference`, already sorted best-first), independents excluded since
// there's no conference championship to win. confChampions (optional, a Map
// of conference -> team, see confChampionsFromRows()) is the actual
// championship-game winner for any conference whose title game has already
// been played this snapshot -- it overrides the rank-based stand-in, since
// the two aren't always the same team (e.g. Clemson beating a higher-ranked
// SMU for the 2024 ACC title).
function conferenceLeaders(rankedTeams, confChampions) {
  const confLeader = new Map();
  for (const r of rankedTeams) {
    if (!r.conference || r.conference === 'FBS Independents') continue;
    if (!confLeader.has(r.conference)) confLeader.set(r.conference, r.team);
  }
  for (const [conf, champ] of confChampions || []) confLeader.set(conf, champ);
  return confLeader;
}

// Returns the projected field for `season` in projected seed order
// (position 0 = #1 seed) -- accuracy.html lines it up slot-for-slot against
// the real bracket. 2024-2025: the 5 highest-ranked conference leaders + the
// next 7 best. 2026+: every Power Four leader regardless of rank + the
// single highest-ranked Group of Six leader, plus Notre Dame if it's inside
// the top 12, with the best remaining teams filling out 12.
function playoffFieldProjection(rankedTeams, season, confChampions) {
  if (season < TWELVE_TEAM_ERA_START) {
    return rankedTeams.slice(0, fieldSizeFor(season)).map(r => r.team);
  }
  // A team missing from rankedTeams ranks after every ranked one, name
  // breaking ties -- same as weekly_update.py's by_rank. (The site always
  // passes every team, so this is only for parity with the Python side.)
  const rankOf = new Map(rankedTeams.map((r, i) => [r.team, i]));
  const rankKey = t => rankOf.has(t) ? rankOf.get(t) : rankedTeams.length;
  const byRank = (a, b) => rankKey(a) - rankKey(b) || (a < b ? -1 : a > b ? 1 : 0);
  const confLeader = conferenceLeaders(rankedTeams, confChampions);

  // Auto bids go by each leader's own rank, not by conference-discovery
  // order -- those can differ once a real champion replaces a
  // higher-ranked stand-in from the same conference.
  let autoBids, guaranteed = [];
  if (season >= P4_AUTO_BID_ERA_START) {
    const entries = [...confLeader];
    const powerFour = entries.filter(([c]) => POWER_FOUR.has(c)).map(([, t]) => t);
    const groupOfSix = entries.filter(([c]) => !POWER_FOUR.has(c)).map(([, t]) => t).sort(byRank).slice(0, 1);
    autoBids = [...powerFour, ...groupOfSix];
    if (rankKey('Notre Dame') < NOTRE_DAME_TOP_N) guaranteed = ['Notre Dame'];
  } else {
    autoBids = [...confLeader.values()].sort(byRank).slice(0, 5);
  }

  // A Set, and filled to 12 from whatever it already holds, so the field is
  // always 12 even when fewer than 5 conferences have a leader or Notre
  // Dame is itself a conference leader (as in 2020).
  const field = new Set([...autoBids, ...guaranteed]);
  for (const r of rankedTeams) {
    if (field.size >= 12) break;
    field.add(r.team);
  }

  if (season >= STRAIGHT_SEEDING_START) return [...field].sort(byRank);
  const byes = autoBids.sort(byRank).slice(0, 4);
  return [...byes, ...[...field].filter(t => !byes.includes(t)).sort(byRank)];
}
