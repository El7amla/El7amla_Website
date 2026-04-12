"""
update_standings.py
====================
El7amla 2v2 Fantasy League — Standings Calculator
Fetches live FPL data and generates current_standings.json

Rules:
  - Player points counted only on gameweeks their team PLAYED (not BYE)
  - Team match points: 3=win, 1=draw, 0=loss  (compared by team total pts that GW)
  - Bonus: every GW the team with highest total pts gets +1 (all teams included,
    even BYE teams). Tie at top = NO bonus awarded that GW.
  - Tiebreaker order: total → matchPts → GD → GF → wins

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


# ─────────────────────────────────────────────
# MAIN CALCULATION
# ─────────────────────────────────────────────

def calculate_standings(current_gw: int) -> list[dict]:
    league   = load_league()
    fixtures = load_fixtures()

    team_names = list(league.keys())

    # ── initialise team stats ──
    stats: dict[str, dict] = {}
    for team in team_names:
        stats[team] = {
            "played":   0,
            "won":      0,
            "draw":     0,
            "lost":     0,
            "gf":       0,   # goals-for  = total FPL pts scored
            "ga":       0,   # goals-against
            "gd":       0,   # goal difference
            "matchPts": 0,
            "bonus":    0,
            "total":    0,
        }

    # ── cache: (entry_id, gw) → points ──
    pts_cache: dict[tuple, int | None] = {}

    def team_gw_points(team_name: str, gw: int) -> int | None:
        """Sum of both players' points for a team in a GW. None = not played."""
        players = league[team_name]["players"]
        total = 0
        for player_name, entry_id in players.items():
            key = (entry_id, gw)
            if key not in pts_cache:
                log.info(f"  Fetching GW{gw} — {team_name} / {player_name} (id={entry_id})")
                pts_cache[key] = get_player_gw_points(entry_id, gw)
                time.sleep(API_DELAY)
            p = pts_cache[key]
            if p is None:
                return None
            total += p
        return total

    # ── per-GW team totals (needed for bonus) ──
    # gw_team_pts[gw][team] = int or None
    gw_team_pts: dict[int, dict[str, int | None]] = {}

    log.info(f"Processing GW 1 → {current_gw}")

    for gw in range(1, current_gw + 1):
        matchups = fixtures.get(gw, [])

        # ── detect full-bye week (GW 11, 22, 38) ──
        all_bye = all(
            (m[0] == "BYE" and m[1] == "BYE") for m in matchups
        )
        if all_bye:
            log.info(f"GW{gw}: Full-bye week — skipping match logic")
            # Still need points for bonus calculation — fetch all teams
            gw_team_pts[gw] = {}
            for team in team_names:
                gw_team_pts[gw][team] = team_gw_points(team, gw)
            # apply bonus only (match points = 0 for everyone)
            _apply_bonus(gw, team_names, gw_team_pts, stats)
            continue

        log.info(f"GW{gw}: Processing {len(matchups)} fixture(s)")

        # ── collect every team's GW points ──
        gw_team_pts[gw] = {}
        for team in team_names:
            gw_team_pts[gw][team] = team_gw_points(team, gw)

        # ── process each matchup ──
        for matchup in matchups:
            home, away = matchup[0], matchup[1]

            # One-sided BYE (team gets a rest, no match recorded)
            if home == "BYE" or away == "BYE":
                continue

            home_pts = gw_team_pts[gw].get(home)
            away_pts = gw_team_pts[gw].get(away)

            if home_pts is None or away_pts is None:
                log.warning(f"  GW{gw}: Missing points for {home} vs {away} — skipping")
                continue

            # update played count
            stats[home]["played"] += 1
            stats[away]["played"] += 1

            # goals for / against
            stats[home]["gf"] += home_pts
            stats[home]["ga"] += away_pts
            stats[away]["gf"] += away_pts
            stats[away]["ga"] += home_pts

            # match result
            if home_pts > away_pts:
                stats[home]["won"]      += 1
                stats[home]["matchPts"] += 3
                stats[away]["lost"]     += 1
                log.info(f"  GW{gw}: {home} {home_pts} – {away_pts} {away}  → WIN {home}")
            elif away_pts > home_pts:
                stats[away]["won"]      += 1
                stats[away]["matchPts"] += 3
                stats[home]["lost"]     += 1
                log.info(f"  GW{gw}: {home} {home_pts} – {away_pts} {away}  → WIN {away}")
            else:
                stats[home]["draw"]     += 1
                stats[home]["matchPts"] += 1
                stats[away]["draw"]     += 1
                stats[away]["matchPts"] += 1
                log.info(f"  GW{gw}: {home} {home_pts} – {away_pts} {away}  → DRAW")

        # ── bonus for this GW ──
        _apply_bonus(gw, team_names, gw_team_pts, stats)

    # ── goal difference & total ──
    for team in team_names:
        s = stats[team]
        s["gd"]    = s["gf"] - s["ga"]
        s["total"] = s["matchPts"] + s["bonus"]

    # ── sort teams ──
    sorted_teams = sorted(
        team_names,
        key=lambda t: (
            -stats[t]["total"],
            -stats[t]["matchPts"],
            -stats[t]["gd"],
            -stats[t]["gf"],
            -stats[t]["won"],
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
    Award +1 bonus to the team with the highest GW points.
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
    Sum each player's points only on gameweeks their team actually played
    (i.e. not a BYE week for that team).
    """

    def team_has_bye(team: str, gw: int) -> bool:
        for matchup in fixtures.get(gw, []):
            if team in matchup:
                # team found — check if opponent is BYE
                return "BYE" in matchup
        # team not in fixtures at all for this GW (full-bye week)
        return True

    player_totals = []

    for team_name, team_data in league.items():
        for player_name, entry_id in team_data["players"].items():
            total = 0
            for gw in range(1, current_gw + 1):
                if team_has_bye(team_name, gw):
                    continue   # skip BYE weeks
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

    # 2. Calculate team standings (also populates pts_cache)
    league   = load_league()
    fixtures = load_fixtures()

    # We run calculate_standings first so pts_cache is populated,
    # then reuse the cache for player standings.
    # To share the cache we use a module-level trick:
    global _shared_cache
    _shared_cache = {}

    # Monkey-patch fpl_get cache into calculate_standings via closure
    team_rows = _run_with_shared_cache(current_gw, league, fixtures)

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


def _run_with_shared_cache(current_gw: int, league: dict, fixtures: dict) -> list:
    """
    Runs the standings calculation while storing all fetched
    (entry_id, gw) → points into _shared_cache for reuse.
    """
    team_names = list(league.keys())

    stats: dict[str, dict] = {
        t: {"played":0,"won":0,"draw":0,"lost":0,
            "gf":0,"ga":0,"gd":0,"matchPts":0,"bonus":0,"total":0}
        for t in team_names
    }

    def team_gw_points(team_name: str, gw: int) -> int | None:
        players = league[team_name]["players"]
        total = 0
        for player_name, entry_id in players.items():
            key = (entry_id, gw)
            if key not in _shared_cache:
                log.info(f"  Fetching GW{gw} — {team_name} / {player_name} (id={entry_id})")
                _shared_cache[key] = get_player_gw_points(entry_id, gw)
                time.sleep(API_DELAY)
            p = _shared_cache[key]
            if p is None:
                return None
            total += p
        return total

    gw_team_pts: dict[int, dict[str, int | None]] = {}

    for gw in range(1, current_gw + 1):
        matchups = fixtures.get(gw, [])

        all_bye = all(m[0] == "BYE" and m[1] == "BYE" for m in matchups)

        gw_team_pts[gw] = {}
        for team in team_names:
            gw_team_pts[gw][team] = team_gw_points(team, gw)

        if all_bye:
            log.info(f"GW{gw}: Full-bye week")
            _apply_bonus(gw, team_names, gw_team_pts, stats)
            continue

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

            if home_pts > away_pts:
                stats[home]["won"]      += 1
                stats[home]["matchPts"] += 3
                stats[away]["lost"]     += 1
                log.info(f"  {home} {home_pts}–{away_pts} {away}  WIN→{home}")
            elif away_pts > home_pts:
                stats[away]["won"]      += 1
                stats[away]["matchPts"] += 3
                stats[home]["lost"]     += 1
                log.info(f"  {home} {home_pts}–{away_pts} {away}  WIN→{away}")
            else:
                stats[home]["draw"]     += 1
                stats[home]["matchPts"] += 1
                stats[away]["draw"]     += 1
                stats[away]["matchPts"] += 1
                log.info(f"  {home} {home_pts}–{away_pts} {away}  DRAW")

        _apply_bonus(gw, team_names, gw_team_pts, stats)

    for team in team_names:
        s = stats[team]
        s["gd"]    = s["gf"] - s["ga"]
        s["total"] = s["matchPts"] + s["bonus"]

    sorted_teams = sorted(
        team_names,
        key=lambda t: (
            -stats[t]["total"],
            -stats[t]["matchPts"],
            -stats[t]["gd"],
            -stats[t]["gf"],
            -stats[t]["won"],
        )
    )

    return [{"name": t, **stats[t]} for t in sorted_teams]


if __name__ == "__main__":
    main()
