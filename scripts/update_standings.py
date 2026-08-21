"""
update_standings.py
====================
El7amla 2v2 Fantasy League — Standings Calculator
Fetches live FPL data and generates current_standings.json

Rules:
  - Player points counted only on gameweeks their team PLAYED (not BYE)
  - Team match points: 3=win, 1=draw, 0=loss  (compared by team total pts that GW,
    AFTER chip adjustments — see Chips System below)
  - Bonus: every GW the team with highest total pts gets +1 (all teams included,
    even BYE teams). Tie at top = NO bonus awarded that GW.
  - Tiebreaker order: total → matchPts → GD → GF → wins

  Chips System (data/chips.json):
    - one_v_one     : 2x/season (once per half: GW1-19, GW20-35). Player vs player
                       duel against the scheduled opponent from fixtures.json. The duel
                       determines the match winner; winner gets the normal 3 league
                       points, loser gets 0. Equal player points = match draw (1 point each).
    - bonus3        : 1x/season only. If the team WINS its fixture that GW, +3 extra
                       league points on top of the normal 3 match points.
    - double_player : 2x/season (once per half). Chosen player's GW points ×2,
                       teammate's points zeroed — affects that team's GW total used
                       for match result (win/draw/loss) and gf/ga.

    Chip effects only apply to TEAM standings (match results / gf / ga / bonus).
    Individual PLAYER standings (calculate_player_standings) stay on raw FPL points,
    unaffected by chips, since they reflect real FPL performance.

Run:
  pip install requests
  python update_standings.py
"""

import json
import time
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent          # → scripts/
REPO_ROOT       = BASE_DIR.parent                # → repo root
LEAGUE_FILE     = REPO_ROOT / "league.json"
FIXTURES_FILE   = REPO_ROOT / "fixtures.json"
CHIPS_FILE      = REPO_ROOT / "data" / "chips.json"
OUTPUT_FILE     = REPO_ROOT / "data" / "current_standings.json"

FPL_BASE        = "https://fantasy.premierleague.com/api"
FPL_BOOTSTRAP   = f"{FPL_BASE}/bootstrap-static/"
FPL_PICKS       = f"{FPL_BASE}/entry/{{entry_id}}/event/{{gw}}/picks/"

# Polite delay between FPL API calls (seconds)
API_DELAY       = 0.8

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; El7amla-Bot/1.0)",
}

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def fpl_get(url: str, retries: int = 3) -> dict | None:
    """GET an FPL endpoint with retries. Returns parsed JSON or None."""
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 404:
                return None   # GW not played yet
            else:
                log.warning(f"HTTP {r.status_code} — {url}  (attempt {attempt})")
        except requests.RequestException as e:
            log.warning(f"Request error — {url}  (attempt {attempt}): {e}")
        if attempt < retries:
            time.sleep(2 ** attempt)
    return None


def get_current_gw() -> int:
    """Return the latest finished gameweek from FPL bootstrap."""
    data = fpl_get(FPL_BOOTSTRAP)
    if not data:
        raise RuntimeError("Cannot fetch FPL bootstrap — check connectivity")
    for event in reversed(data["events"]):
        if event["finished"]:
            return event["id"]
    return 1


def get_player_gw_points(entry_id: int, gw: int) -> int | None:
    """
    Return the net points for an FPL entry in a given gameweek.
    Net = active_chip adjusted points minus transfer cost.
    Returns None if the GW hasn't been played yet.
    """
    url = FPL_PICKS.format(entry_id=entry_id, gw=gw)
    data = fpl_get(url)
    if data is None:
        return None

    entry_history = data.get("entry_history", {})
    points        = entry_history.get("points", 0)
    # transfer cost is already negative in 'event_transfers_cost'
    transfer_cost = entry_history.get("event_transfers_cost", 0)
    net_points    = points - transfer_cost
    return net_points


# ─────────────────────────────────────────────
# LOAD LOCAL FILES
# ─────────────────────────────────────────────

def load_league() -> dict:
    with open(LEAGUE_FILE, encoding="utf-8") as f:
        return json.load(f)["teams"]


def load_fixtures() -> dict[int, list]:
    with open(FIXTURES_FILE, encoding="utf-8") as f:
        raw = json.load(f)["fixtures"]
    return {int(gw): matchups for gw, matchups in raw.items()}


def load_chips() -> dict:
    """
    Loads data/chips.json (written by khawas.html / the team dashboard).
    Returns {} if the file doesn't exist yet — chips are optional, the
    script must still work fine before anyone activates anything.
    """
    if not CHIPS_FILE.exists():
        log.info("chips.json not found — running without chip adjustments")
        return {}
    with open(CHIPS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    data.pop("_comment", None)
    return data


# ─────────────────────────────────────────────
# CHIPS SYSTEM — HELPERS
# ─────────────────────────────────────────────

def half_key(gw: int) -> str:
    """GW1-19 = الدور الأول (h1), GW20-35 = الدور الثاني (h2)."""
    return "h1" if gw <= 19 else "h2"


def get_chip_slot(chips: dict, team: str, chip_key: str, gw: int) -> dict | None:
    """
    Returns the activated chip record for `team`/`chip_key` IF it was
    activated for this exact `gw` and its status is "used" (not "canceled").
    Returns None otherwise (not activated, wrong GW, or mutually canceled).
    """
    team_chips = chips.get(team)
    if not team_chips:
        return None

    if chip_key == "bonus3":
        slot = team_chips.get("bonus3")
        if slot and slot.get("used") and slot.get("gw") == gw and slot.get("status") == "used":
            return slot
        return None

    slot = (team_chips.get(chip_key) or {}).get(half_key(gw))
    if slot and slot.get("gw") == gw and slot.get("status") == "used":
        return slot
    return None


def get_scheduled_opponent(fixtures: dict, team: str, gw: int) -> str | None:
    """Return the team scheduled to play `team` in `gw`, or None."""
    for matchup in fixtures.get(gw, []):
        if len(matchup) < 2:
            continue
        home, away = matchup[0], matchup[1]
        if home == team and away != "BYE":
            return away
        if away == team and home != "BYE":
            return home
    return None


def get_1v1_duel_for_match(
    chips: dict, fixtures: dict, home: str, away: str, gw: int, raw_player_pts: dict
) -> tuple[str, dict] | None:
    """Resolve a valid 1v1 activation for this fixture."""
    home_duel = get_chip_slot(chips, home, "one_v_one", gw)
    away_duel = get_chip_slot(chips, away, "one_v_one", gw)
    if home_duel and away_duel:
        log.info(f"  GW{gw}: 1v1 mutual activation {home} vs {away} — both canceled")
        return None

    if home_duel:
        team, opponent, duel = home, away, home_duel
    elif away_duel:
        team, opponent, duel = away, home, away_duel
    else:
        return None

    if get_scheduled_opponent(fixtures, team, gw) != opponent:
        log.warning(f"  GW{gw}: 1v1 fixture mismatch for {team} — ignoring activation")
        return None

    my_player = duel.get("myPlayer")
    opp_player = duel.get("oppPlayer")
    my_pts = (raw_player_pts.get(team) or {}).get(my_player)
    opp_pts = (raw_player_pts.get(opponent) or {}).get(opp_player)
    if my_pts is None or opp_pts is None:
        log.warning(f"  GW{gw}: 1v1 missing player data for {team} vs {opponent} — ignoring activation")
        return None

    return team, {"opponent": opponent, "myPlayer": my_player, "oppPlayer": opp_player, "myPts": my_pts, "oppPts": opp_pts}


def team_player_points_dict(team_name: str, gw: int, league: dict, cache: dict) -> dict | None:
    """
    Returns {player_name: raw_points} for the team's two players this GW,
    or None if either player's GW data isn't available yet.
    Uses/populates the shared (entry_id, gw) → points cache.
    """
    players = league[team_name]["players"]
    result = {}
    for player_name, entry_id in players.items():
        key = (entry_id, gw)
        if key not in cache:
            log.info(f"  Fetching GW{gw} — {team_name} / {player_name} (id={entry_id})")
            cache[key] = get_player_gw_points(entry_id, gw)
            time.sleep(API_DELAY)
        p = cache[key]
        if p is None:
            return None
        result[player_name] = p
    return result


def compute_adjusted_team_points(
    team: str,
    gw: int,
    raw_player_pts: dict,
    chips: dict,
) -> int | None:
    """
    Returns the team's chip-adjusted total points for this GW (used for
    match result / gf / ga / overall bonus). None if the team's own raw
    data isn't available yet for this GW.
    """
    own = raw_player_pts.get(team)
    if own is None:
        return None

    adjusted = dict(own)

    # ── دبل نقاط لاعب ──
    dbl = get_chip_slot(chips, team, "double_player", gw)
    if dbl:
        doubled = dbl.get("doubledPlayer")
        zeroed  = dbl.get("zeroedPlayer")
        if doubled in adjusted:
            adjusted[doubled] = adjusted[doubled] * 2
        if zeroed in adjusted:
            adjusted[zeroed] = 0
        log.info(f"  GW{gw}: {team} double_player → {doubled} ×2, {zeroed} = 0")

    return sum(adjusted.values())


def apply_bonus3_if_won(chips: dict, team: str, gw: int, stats: dict) -> None:
    """+3 extra league points if this team's bonus3 chip is active AND they won this GW."""
    slot = get_chip_slot(chips, team, "bonus3", gw)
    if slot:
        stats[team]["chipBonus"] += 3
        log.info(f"  GW{gw}: بونص X3 → {team} +3 نقطة إضافية")


# ─────────────────────────────────────────────
# MAIN CALCULATION  (legacy standalone version — kept in sync with
# _run_with_shared_cache below, which is the one actually used by main())
# ─────────────────────────────────────────────

def calculate_standings(current_gw: int, chips: dict | None = None) -> list[dict]:
    league   = load_league()
    fixtures = load_fixtures()
    chips    = chips if chips is not None else load_chips()

    team_names = list(league.keys())

    # ── initialise team stats ──
    stats: dict[str, dict] = {}
    for team in team_names:
        stats[team] = {
            "played":    0,
            "won":       0,
            "draw":      0,
            "lost":      0,
            "gf":        0,   # goals-for  = total FPL pts scored
            "ga":        0,   # goals-against
            "gd":        0,   # goal difference
            "matchPts":  0,
            "bonus":     0,
            "chipBonus": 0,   # extra points from bonus3 only; 1v1 uses matchPts
            "total":     0,
        }

    # ── cache: (entry_id, gw) → points ──
    pts_cache: dict[tuple, int | None] = {}

    # ── per-GW team totals (needed for bonus) ──
    gw_team_pts: dict[int, dict[str, int | None]] = {}

    log.info(f"Processing GW 1 → {current_gw}")

    for gw in range(1, current_gw + 1):
        matchups = fixtures.get(gw, [])


        # ── raw per-player points (needed for chip adjustments) ──
        raw_player_pts = {}
        for team in team_names:
            raw_player_pts[team] = team_player_points_dict(team, gw, league, pts_cache)

        gw_team_pts[gw] = {}
        for team in team_names:
            gw_team_pts[gw][team] = compute_adjusted_team_points(team, gw, raw_player_pts, chips)

        log.info(f"GW{gw}: Processing {len(matchups)} fixture(s)")

        for matchup in matchups:
            home, away = matchup[0], matchup[1]
            if home == "BYE" or away == "BYE":
                continue

            home_pts = gw_team_pts[gw].get(home)
            away_pts = gw_team_pts[gw].get(away)

            if home_pts is None or away_pts is None:
                log.warning(f"  GW{gw}: Missing points for {home} vs {away} — skipping")
                continue

            stats[home]["played"] += 1
            stats[away]["played"] += 1
            stats[home]["gf"] += home_pts
            stats[home]["ga"] += away_pts
            stats[away]["gf"] += away_pts
            stats[away]["ga"] += home_pts

            duel_result = get_1v1_duel_for_match(chips, fixtures, home, away, gw, raw_player_pts)

            if duel_result:
                duel_team, duel = duel_result
                if duel["myPts"] > duel["oppPts"]:
                    winner, loser = duel_team, duel["opponent"]
                    stats[winner]["won"] += 1
                    stats[winner]["matchPts"] += 3
                    stats[loser]["lost"] += 1
                    apply_bonus3_if_won(chips, winner, gw, stats)
                    log.info(f"  GW{gw}: 1v1 WIN → {winner} ({duel['myPlayer']} {duel['myPts']}) vs {loser} ({duel['oppPlayer']} {duel['oppPts']})")
                elif duel["oppPts"] > duel["myPts"]:
                    winner, loser = duel["opponent"], duel_team
                    stats[winner]["won"] += 1
                    stats[winner]["matchPts"] += 3
                    stats[loser]["lost"] += 1
                    apply_bonus3_if_won(chips, winner, gw, stats)
                    log.info(f"  GW{gw}: 1v1 LOSS → {duel_team}; WIN → {winner}")
                else:
                    stats[home]["draw"] += 1
                    stats[home]["matchPts"] += 1
                    stats[away]["draw"] += 1
                    stats[away]["matchPts"] += 1
                    log.info(f"  GW{gw}: 1v1 DRAW → {home} vs {away} ({duel['myPts']}-{duel['oppPts']})")
            elif home_pts > away_pts:
                stats[home]["won"]      += 1
                stats[home]["matchPts"] += 3
                stats[away]["lost"]     += 1
                apply_bonus3_if_won(chips, home, gw, stats)
                log.info(f"  GW{gw}: {home} {home_pts} – {away_pts} {away}  → WIN {home}")
            elif away_pts > home_pts:
                stats[away]["won"]      += 1
                stats[away]["matchPts"] += 3
                stats[home]["lost"]     += 1
                apply_bonus3_if_won(chips, away, gw, stats)
                log.info(f"  GW{gw}: {home} {home_pts} – {away_pts} {away}  → WIN {away}")
            else:
                stats[home]["draw"]     += 1
                stats[home]["matchPts"] += 1
                stats[away]["draw"]     += 1
                stats[away]["matchPts"] += 1
                log.info(f"  GW{gw}: {home} {home_pts} – {away_pts} {away}  → DRAW")

        # ── 1v1 duels (independent of fixtures) ──

        # ── bonus for this GW ──
        _apply_bonus(gw, team_names, gw_team_pts, stats)

    # ── goal difference & total ──
    for team in team_names:
        s = stats[team]
        s["gd"]    = s["gf"] - s["ga"]
        s["total"] = s["matchPts"] + s["bonus"] + s["chipBonus"]

    # ── sort teams ──
    sorted_teams = sorted(
        team_names,
        key=lambda t: (
            -stats[t]["total"],   # 1. الإجمالي
            -stats[t]["gd"],      # 2. فارق النقاط
            -stats[t]["matchPts"],# 3. نقاط الماتشات
            -stats[t]["gf"],      # 4. النقاط له
            -stats[t]["won"],     # 5. عدد الفوز
        )
    )

    return [{"name": t, **stats[t]} for t in sorted_teams]


def _apply_bonus(
    gw: int,
    team_names: list[str],
    gw_team_pts: dict,
    stats: dict,
) -> None:
    """
    Award +1 bonus to the team with the highest GW points (chip-adjusted).
    Rules:
      - All teams compete (including BYE teams).
      - If the top score is shared by 2+ teams → NO bonus awarded.
      - None (unplayed) scores are ignored.
    """
    scored = {
        t: pts
        for t in team_names
        if (pts := gw_team_pts[gw].get(t)) is not None
    }
    if not scored:
        return

    max_pts = max(scored.values())
    top_teams = [t for t, p in scored.items() if p == max_pts]

    if len(top_teams) == 1:
        winner = top_teams[0]
        stats[winner]["bonus"] += 1
        log.info(f"  GW{gw}: Bonus → {winner} ({max_pts} pts)")
    else:
        log.info(f"  GW{gw}: No bonus — tie at {max_pts} pts between {top_teams}")


# ─────────────────────────────────────────────
# PLAYER STANDINGS
# ─────────────────────────────────────────────

def calculate_player_standings(
    current_gw: int,
    fixtures: dict,
    league: dict,
    pts_cache: dict,
) -> list[dict]:
    """
    Sum each player's RAW points only on gameweeks their team actually played
    (i.e. not a BYE week for that team). Deliberately NOT chip-adjusted —
    this reflects real FPL performance, independent of fantasy-league chips.
    """

    def team_has_bye(team: str, gw: int) -> bool:
        for matchup in fixtures.get(gw, []):
            if team in matchup:
                return "BYE" in matchup
        return True

    player_totals = []

    for team_name, team_data in league.items():
        for player_name, entry_id in team_data["players"].items():
            total = 0
            for gw in range(1, current_gw + 1):
                if team_has_bye(team_name, gw):
                    continue
                pts = pts_cache.get((entry_id, gw))
                if pts is not None:
                    total += pts

            player_totals.append({
                "name":   player_name,
                "team":   team_name,
                "points": total,
            })

    return sorted(player_totals, key=lambda p: -p["points"])


# ─────────────────────────────────────────────
# OUTPUT
# ─────────────────────────────────────────────

def write_output(team_rows: list, player_rows: list, current_gw: int) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "current_gw":   current_gw,
        "last_updated": now,
        "teams":        team_rows,
        "players":      player_rows,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info(f"Written → {OUTPUT_FILE}")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

def main():
    log.info("═" * 55)
    log.info("El7amla Standings Updater — start")
    log.info("═" * 55)

    # 1. Detect current gameweek
    log.info("Fetching current gameweek from FPL bootstrap…")
    current_gw = get_current_gw()
    log.info(f"Current GW: {current_gw}")

    # 2. Load league / fixtures / chips
    league   = load_league()
    fixtures = load_fixtures()
    chips    = load_chips()

    # The league season length comes from fixtures.json, so the FPL calendar
    # cannot make this script process GWs beyond the league season.
    season_gws = sorted(fixtures)
    if not season_gws:
        raise RuntimeError("fixtures.json contains no gameweeks")
    season_max_gw = season_gws[-1]
    if current_gw > season_max_gw:
        log.info(f"Capping FPL GW {current_gw} to league season GW {season_max_gw}")
        current_gw = season_max_gw

    # We run _run_with_shared_cache first so pts_cache is populated,
    # then reuse the cache for player standings.
    global _shared_cache
    _shared_cache = {}

    team_rows = _run_with_shared_cache(current_gw, league, fixtures, chips)

    # 3. Player standings (reuse cache — zero extra API calls)
    log.info("Calculating player standings…")
    player_rows = calculate_player_standings(
        current_gw, fixtures, league, _shared_cache
    )

    # 4. Write output
    write_output(team_rows, player_rows, current_gw)

    log.info("═" * 55)
    log.info("Done ✓")
    log.info("═" * 55)


# ── shared cache bridge ──────────────────────

_shared_cache: dict = {}


def _run_with_shared_cache(current_gw: int, league: dict, fixtures: dict, chips: dict) -> list:
    """
    Runs the standings calculation while storing all fetched
    (entry_id, gw) → points into _shared_cache for reuse.
    This is the version actually called by main().
    """
    team_names = list(league.keys())

    stats: dict[str, dict] = {
        t: {"played":0,"won":0,"draw":0,"lost":0,
            "gf":0,"ga":0,"gd":0,"matchPts":0,"bonus":0,"chipBonus":0,"total":0}
        for t in team_names
    }

    gw_team_pts: dict[int, dict[str, int | None]] = {}

    for gw in range(1, current_gw + 1):
        matchups = fixtures.get(gw, [])

        # ── raw per-player points this GW (drives chip adjustments + history) ──
        raw_player_pts = {}
        for team in team_names:
            raw_player_pts[team] = team_player_points_dict(team, gw, league, _shared_cache)

        # ── chip-adjusted team totals (double_player applied here) ──
        gw_team_pts[gw] = {}
        for team in team_names:
            gw_team_pts[gw][team] = compute_adjusted_team_points(team, gw, raw_player_pts, chips)

        # ── Save per-GW history file for the teams page (raw, unadjusted) ──
        gw_hist = {}
        for team in team_names:
            gw_hist[team] = {}
            for player_name, entry_id in league[team]["players"].items():
                key = (entry_id, gw)
                pts = _shared_cache.get(key)
                if pts is not None:
                    gw_hist[team][str(entry_id)] = {"points": pts}
        gw_hist_dir  = REPO_ROOT / "data" / "gw_history"
        gw_hist_dir.mkdir(parents=True, exist_ok=True)
        gw_hist_file = gw_hist_dir / f"gw{gw}.json"
        with open(gw_hist_file, "w", encoding="utf-8") as f:
            json.dump(gw_hist, f, ensure_ascii=False, indent=2)


        log.info(f"GW{gw}: Processing fixtures…")

        for matchup in matchups:
            home, away = matchup[0], matchup[1]
            if home == "BYE" or away == "BYE":
                continue

            home_pts = gw_team_pts[gw].get(home)
            away_pts = gw_team_pts[gw].get(away)

            if home_pts is None or away_pts is None:
                log.warning(f"  GW{gw}: Missing pts for {home} vs {away} — skipping")
                continue

            stats[home]["played"] += 1
            stats[away]["played"] += 1
            stats[home]["gf"]     += home_pts
            stats[home]["ga"]     += away_pts
            stats[away]["gf"]     += away_pts
            stats[away]["ga"]     += home_pts

            duel_result = get_1v1_duel_for_match(chips, fixtures, home, away, gw, raw_player_pts)

            if duel_result:
                duel_team, duel = duel_result
                if duel["myPts"] > duel["oppPts"]:
                    winner, loser = duel_team, duel["opponent"]
                    stats[winner]["won"] += 1
                    stats[winner]["matchPts"] += 3
                    stats[loser]["lost"] += 1
                    apply_bonus3_if_won(chips, winner, gw, stats)
                    log.info(f"  GW{gw}: 1v1 WIN → {winner} ({duel['myPlayer']} {duel['myPts']}) vs {loser} ({duel['oppPlayer']} {duel['oppPts']})")
                elif duel["oppPts"] > duel["myPts"]:
                    winner, loser = duel["opponent"], duel_team
                    stats[winner]["won"] += 1
                    stats[winner]["matchPts"] += 3
                    stats[loser]["lost"] += 1
                    apply_bonus3_if_won(chips, winner, gw, stats)
                    log.info(f"  GW{gw}: 1v1 LOSS → {duel_team}; WIN → {winner}")
                else:
                    stats[home]["draw"] += 1
                    stats[home]["matchPts"] += 1
                    stats[away]["draw"] += 1
                    stats[away]["matchPts"] += 1
                    log.info(f"  GW{gw}: 1v1 DRAW → {home} vs {away} ({duel['myPts']}-{duel['oppPts']})")
            elif home_pts > away_pts:
                stats[home]["won"]      += 1
                stats[home]["matchPts"] += 3
                stats[away]["lost"]     += 1
                apply_bonus3_if_won(chips, home, gw, stats)
                log.info(f"  GW{gw}: {home} {home_pts} – {away_pts} {away}  → WIN {home}")
            elif away_pts > home_pts:
                stats[away]["won"]      += 1
                stats[away]["matchPts"] += 3
                stats[home]["lost"]     += 1
                apply_bonus3_if_won(chips, away, gw, stats)
                log.info(f"  GW{gw}: {home} {home_pts} – {away_pts} {away}  → WIN {away}")
            else:
                stats[home]["draw"]     += 1
                stats[home]["matchPts"] += 1
                stats[away]["draw"]     += 1
                stats[away]["matchPts"] += 1
                log.info(f"  GW{gw}: {home} {home_pts} – {away_pts} {away}  → DRAW")

        # ── 1v1 duels — independent of who plays whom this GW ──

        _apply_bonus(gw, team_names, gw_team_pts, stats)

    for team in team_names:
        s = stats[team]
        s["gd"]    = s["gf"] - s["ga"]
        s["total"] = s["matchPts"] + s["bonus"] + s["chipBonus"]

    sorted_teams = sorted(
        team_names,
        key=lambda t: (
            -stats[t]["total"],   # 1. الإجمالي
            -stats[t]["gd"],      # 2. فارق النقاط
            -stats[t]["matchPts"],# 3. نقاط الماتشات
            -stats[t]["gf"],      # 4. النقاط له
            -stats[t]["won"],     # 5. عدد الفوز
        )
    )

    return [{"name": t, **stats[t]} for t in sorted_teams]


if __name__ == "__main__":
    main()
