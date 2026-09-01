# update_standings.py
# ====================
# El7amla 2v2 Fantasy League — Standings Calculator
# Fetches FPL data and generates current_standings.json
#
# UPDATED:
#   - Adds match results to current_standings.json
#   - Each match contains:
#       gw
#       home
#       away
#       homePts
#       awayPts
#       result
#       winner
#   - Keeps existing team standings
#   - Keeps existing player standings
#   - Keeps chips system
#   - Keeps GW history files
#
# Rules:
#   - Player points counted only on gameweeks their team PLAYED (not BYE)
#   - Team match points:
#         3 = win
#         1 = draw
#         0 = loss
#
#   - NO regular GW bonus.
#
# Chips System:
#   - one_v_one:
#       2x/season
#       once per half: GW1-19, GW20-35
#
#   - bonus3:
#       1x/season
#       If the team wins its fixture that GW,
#       +3 extra league points.
#
#   - double_player:
#       2x/season
#       once per half.
#
# Final Team Total:
#     total = matchPts + chipBonus


import json
import time
import logging
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

BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parent

LEAGUE_FILE = REPO_ROOT / "league.json"
FIXTURES_FILE = REPO_ROOT / "fixtures.json"
CHIPS_FILE = REPO_ROOT / "data" / "chips.json"
OUTPUT_FILE = REPO_ROOT / "data" / "current_standings.json"
LINEUPS_DIR = REPO_ROOT / "data" / "gw_lineups"

FPL_BASE = "https://fantasy.premierleague.com/api"
FPL_BOOTSTRAP = f"{FPL_BASE}/bootstrap-static/"
FPL_PICKS = f"{FPL_BASE}/entry/{{entry_id}}/event/{{gw}}/picks/"
FPL_EVENT_LIVE = f"{FPL_BASE}/event/{{gw}}/live/"

API_DELAY = 0.8

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; El7amla-Bot/1.0)",
}

_fpl_players: dict[int, dict] = {}
_entry_gw_cache: dict[tuple[int, int], dict | None] = {}

_fpl_players: dict[int, dict] = {}
_entry_gw_cache: dict[tuple[int, int], dict | None] = {}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def fpl_get(url: str, retries: int = 3) -> dict | None:
    """
    GET an FPL endpoint with retries.
    Returns parsed JSON or None.
    """

    for attempt in range(1, retries + 1):

        try:
            response = requests.get(
                url,
                headers=HEADERS,
                timeout=15,
            )

            if response.status_code == 200:
                return response.json()

            if response.status_code == 404:
                return None

            log.warning(
                f"HTTP {response.status_code} — "
                f"{url} "
                f"(attempt {attempt})"
            )

        except requests.RequestException as exc:

            log.warning(
                f"Request error — "
                f"{url} "
                f"(attempt {attempt}): {exc}"
            )

        if attempt < retries:
            time.sleep(2 ** attempt)

    return None


def get_current_gw() -> int:
    """Return the latest finished gameweek and cache FPL player metadata."""
    data = fpl_get(FPL_BOOTSTRAP)
    if not data:
        raise RuntimeError("Cannot fetch FPL bootstrap — check connectivity")

    global _fpl_players
    _fpl_players = {}
    for player in data.get("elements", []):
        try:
            element_id = int(player["id"])
        except (KeyError, TypeError, ValueError):
            continue
        _fpl_players[element_id] = {
            "id": element_id,
            "web_name": player.get("web_name", ""),
            "first_name": player.get("first_name", ""),
            "second_name": player.get("second_name", ""),
            "position": player.get("element_type"),
        }

    for event in reversed(data.get("events", [])):
        if event.get("finished"):
            return event["id"]
    return 1


def get_gw_live_points(gw: int) -> dict[int, int]:
    """Return FPL player total points for a completed GW."""
    data = fpl_get(FPL_EVENT_LIVE.format(gw=gw))
    if not data:
        return {}
    result = {}
    for item in data.get("elements", []):
        try:
            element_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        stats = item.get("stats") or {}
        result[element_id] = int(stats.get("total_points", 0) or 0)
    return result


def get_player_gw_picks(
    entry_id: int,
    gw: int,
    live_points: dict[int, int] | None = None,
) -> dict | None:
    """Fetch complete FPL lineup, captain/vice-captain and player points."""
    key = (entry_id, gw)
    if key not in _entry_gw_cache:
        _entry_gw_cache[key] = fpl_get(
            FPL_PICKS.format(entry_id=entry_id, gw=gw)
        )

    data = _entry_gw_cache[key]
    if data is None:
        return None

    picks = []
    for pick in data.get("picks", []):
        try:
            element_id = int(pick.get("element"))
        except (TypeError, ValueError):
            continue
        meta = _fpl_players.get(element_id, {})
        picks.append({
            "element": element_id,
            "position": pick.get("position"),
            "multiplier": pick.get("multiplier", 1),
            "is_captain": bool(pick.get("is_captain")),
            "is_vice_captain": bool(pick.get("is_vice_captain")),
            "player": meta.get("web_name") or meta.get("second_name") or str(element_id),
            "first_name": meta.get("first_name", ""),
            "second_name": meta.get("second_name", ""),
            "element_type": meta.get("position"),
            "points": (live_points or {}).get(element_id, 0),
        })

    entry_history = data.get("entry_history", {})
    return {
        "entry_id": entry_id,
        "gw": gw,
        "points": entry_history.get("points", 0),
        "net_points": entry_history.get("points", 0) - entry_history.get("event_transfers_cost", 0),
        "transfer_cost": entry_history.get("event_transfers_cost", 0),
        "bank": entry_history.get("bank"),
        "value": entry_history.get("value"),
        "picks": picks,
    }


def get_player_gw_points(
    entry_id: int,
    gw: int,
) -> int | None:
    """
    Return the net points for an FPL entry in a given GW.

    FPL points minus transfer cost.
    """

    key = (entry_id, gw)
    if key not in _entry_gw_cache:
        _entry_gw_cache[key] = fpl_get(
            FPL_PICKS.format(entry_id=entry_id, gw=gw)
        )

    data = _entry_gw_cache[key]
    if data is None:
        return None

    entry_history = data.get(
        "entry_history",
        {},
    )

    points = entry_history.get(
        "points",
        0,
    )

    transfer_cost = entry_history.get(
        "event_transfers_cost",
        0,
    )

    return points - transfer_cost


# ─────────────────────────────────────────────
# LOAD LOCAL FILES
# ─────────────────────────────────────────────

def load_league() -> dict:
    """
    Load league.json and return teams.
    """

    with open(
        LEAGUE_FILE,
        encoding="utf-8",
    ) as file:

        return json.load(file)["teams"]


def load_fixtures() -> dict[int, list]:
    """
    Load fixtures.json.

    Returns:
        {
            1: [...],
            2: [...],
            ...
        }
    """

    with open(
        FIXTURES_FILE,
        encoding="utf-8",
    ) as file:

        raw = json.load(file)["fixtures"]

    return {
        int(gw): matchups
        for gw, matchups in raw.items()
    }


def load_chips() -> dict:
    """
    Load data/chips.json.

    Returns {} if the file doesn't exist.
    """

    if not CHIPS_FILE.exists():

        log.info(
            "chips.json not found — "
            "running without chip adjustments"
        )

        return {}

    with open(
        CHIPS_FILE,
        encoding="utf-8",
    ) as file:

        data = json.load(file)

    data.pop("_comment", None)

    return data


# ─────────────────────────────────────────────
# CHIPS SYSTEM
# ─────────────────────────────────────────────

def half_key(gw: int) -> str:
    """
    GW1-19 = first half
    GW20-35 = second half
    """

    return "h1" if gw <= 19 else "h2"


def get_chip_slot(
    chips: dict,
    team: str,
    chip_key: str,
    gw: int,
) -> dict | None:
    """
    Return the activated chip record.
    """

    team_chips = chips.get(team)

    if not team_chips:
        return None

    # bonus3 is single-use
    if chip_key == "bonus3":

        slot = team_chips.get("bonus3")

        if (
            slot
            and slot.get("used")
            and slot.get("gw") == gw
            and slot.get("status") == "used"
        ):
            return slot

        return None

    slot = (
        team_chips.get(chip_key) or {}
    ).get(
        half_key(gw)
    )

    if (
        slot
        and slot.get("gw") == gw
        and slot.get("status") == "used"
    ):
        return slot

    return None


# ─────────────────────────────────────────────
# FIXTURE / 1v1 HELPERS
# ─────────────────────────────────────────────

def get_scheduled_opponent(
    fixtures: dict,
    team: str,
    gw: int,
) -> str | None:
    """
    Return the scheduled opponent.
    """

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
    chips: dict,
    fixtures: dict,
    home: str,
    away: str,
    gw: int,
    raw_player_pts: dict,
) -> tuple[str, dict] | None:
    """
    Resolve a valid 1v1 activation.
    """

    home_duel = get_chip_slot(
        chips,
        home,
        "one_v_one",
        gw,
    )

    away_duel = get_chip_slot(
        chips,
        away,
        "one_v_one",
        gw,
    )

    # Both activated
    if home_duel and away_duel:

        log.info(
            f"GW{gw}: 1v1 mutual activation "
            f"{home} vs {away} — both canceled"
        )

        return None

    if home_duel:

        team = home
        opponent = away
        duel = home_duel

    elif away_duel:

        team = away
        opponent = home
        duel = away_duel

    else:

        return None

    if (
        get_scheduled_opponent(
            fixtures,
            team,
            gw,
        )
        != opponent
    ):

        log.warning(
            f"GW{gw}: 1v1 fixture mismatch "
            f"for {team} — ignoring activation"
        )

        return None

    my_player = duel.get("myPlayer")
    opp_player = duel.get("oppPlayer")

    my_pts = (
        raw_player_pts.get(team) or {}
    ).get(my_player)

    opp_pts = (
        raw_player_pts.get(opponent) or {}
    ).get(opp_player)

    if my_pts is None or opp_pts is None:

        log.warning(
            f"GW{gw}: 1v1 missing player data "
            f"for {team} vs {opponent}"
        )

        return None

    return team, {
        "opponent": opponent,
        "myPlayer": my_player,
        "oppPlayer": opp_player,
        "myPts": my_pts,
        "oppPts": opp_pts,
    }


# ─────────────────────────────────────────────
# PLAYER POINTS
# ─────────────────────────────────────────────

def team_player_points_dict(
    team_name: str,
    gw: int,
    league: dict,
    cache: dict,
) -> dict | None:
    """
    Return:

        {
            player_name: raw_points
        }

    Uses shared cache.
    """

    players = league[team_name]["players"]

    result = {}

    for player_name, entry_id in players.items():

        key = (
            entry_id,
            gw,
        )

        if key not in cache:

            log.info(
                f"Fetching GW{gw} — "
                f"{team_name} / {player_name} "
                f"(id={entry_id})"
            )

            cache[key] = get_player_gw_points(
                entry_id,
                gw,
            )

            time.sleep(API_DELAY)

        points = cache[key]

        if points is None:
            return None

        result[player_name] = points

    return result


# ─────────────────────────────────────────────
# TEAM POINT CALCULATION
# ─────────────────────────────────────────────

def compute_adjusted_team_points(
    team: str,
    gw: int,
    raw_player_pts: dict,
    chips: dict,
) -> int | None:
    """
    Double Player affects team GW points.

    Used for:
      - match result
      - GF
      - GA
    """

    own = raw_player_pts.get(team)

    if own is None:
        return None

    adjusted = dict(own)

    dbl = get_chip_slot(
        chips,
        team,
        "double_player",
        gw,
    )

    if dbl:

        doubled = dbl.get(
            "doubledPlayer"
        )

        zeroed = dbl.get(
            "zeroedPlayer"
        )

        if doubled in adjusted:
            adjusted[doubled] *= 2

        if zeroed in adjusted:
            adjusted[zeroed] = 0

        log.info(
            f"GW{gw}: {team} double_player → "
            f"{doubled} ×2, "
            f"{zeroed} = 0"
        )

    return sum(adjusted.values())


# ─────────────────────────────────────────────
# BONUS3
# ─────────────────────────────────────────────

def apply_bonus3_if_won(
    chips: dict,
    team: str,
    gw: int,
    stats: dict,
) -> None:
    """
    Bonus3 gives +3 league points if the team wins.
    """

    slot = get_chip_slot(
        chips,
        team,
        "bonus3",
        gw,
    )

    if slot:

        stats[team]["chipBonus"] += 3

        log.info(
            f"GW{gw}: Bonus3 → "
            f"{team} +3 extra league points"
        )


# ─────────────────────────────────────────────
# MATCH RESULTS
# ─────────────────────────────────────────────

def build_match_result(
    gw: int,
    home: str,
    away: str,
    home_pts: int,
    away_pts: int,
    duel_result=None,
) -> dict:
    """
    Build a JSON-safe match result.

    This is used by standings.html to display
    actual scores instead of only fixtures.
    """

    winner = None

    if duel_result:

        duel_team, duel = duel_result

        if duel["myPts"] > duel["oppPts"]:
            winner = duel_team

        elif duel["oppPts"] > duel["myPts"]:
            winner = duel["opponent"]

        result_type = (
            "home_win"
            if winner == home
            else "away_win"
            if winner == away
            else "draw"
        )

        return {
            "gw": gw,
            "home": home,
            "away": away,
            "homePts": home_pts,
            "awayPts": away_pts,
            "result": result_type,
            "winner": winner,
            "type": "1v1",
            "duel": {
                "team": duel_team,
                "myPlayer": duel["myPlayer"],
                "oppPlayer": duel["oppPlayer"],
                "myPts": duel["myPts"],
                "oppPts": duel["oppPts"],
            },
        }

    if home_pts > away_pts:

        winner = home
        result_type = "home_win"

    elif away_pts > home_pts:

        winner = away
        result_type = "away_win"

    else:

        result_type = "draw"

    return {
        "gw": gw,
        "home": home,
        "away": away,
        "homePts": home_pts,
        "awayPts": away_pts,
        "result": result_type,
        "winner": winner,
        "type": "normal",
    }


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
    Sum raw player points only for GWs
    where the player's team actually played.
    """

    def team_has_bye(
        team: str,
        gw: int,
    ) -> bool:

        for matchup in fixtures.get(
            gw,
            [],
        ):

            if team in matchup:

                return "BYE" in matchup

        return True

    player_totals = []

    for team_name, team_data in league.items():

        for player_name, entry_id in team_data["players"].items():

            total = 0

            for gw in range(
                1,
                current_gw + 1,
            ):

                if team_has_bye(
                    team_name,
                    gw,
                ):
                    continue

                pts = pts_cache.get(
                    (
                        entry_id,
                        gw,
                    )
                )

                if pts is not None:
                    total += pts

            player_totals.append(
                {
                    "name": player_name,
                    "team": team_name,
                    "points": total,
                }
            )

    return sorted(
        player_totals,
        key=lambda p: -p["points"],
    )


# ─────────────────────────────────────────────
# OUTPUT
# ─────────────────────────────────────────────

def write_output(
    team_rows: list,
    player_rows: list,
    matches: list,
    current_gw: int,
) -> None:
    """
    Write current_standings.json.

    NEW:
      matches = all processed match results.
    """

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    now = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    payload = {
        "current_gw": current_gw,
        "last_updated": now,

        "teams": team_rows,

        "players": player_rows,

        # NEW
        "matches": matches,
    }

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            payload,
            file,
            ensure_ascii=False,
            indent=2,
        )

    log.info(
        f"Written → {OUTPUT_FILE}"
    )


# ─────────────────────────────────────────────
# MAIN CALCULATOR
# ─────────────────────────────────────────────

def _run_with_shared_cache(
    current_gw: int,
    league: dict,
    fixtures: dict,
    chips: dict,
) -> tuple[list, list]:
    """
    Calculate team standings and match results.

    Returns:
        (
            team_rows,
            match_results
        )
    """

    team_names = list(
        league.keys()
    )

    stats = {
        team: {
            "played": 0,
            "won": 0,
            "draw": 0,
            "lost": 0,

            "gf": 0,
            "ga": 0,
            "gd": 0,

            "matchPts": 0,

            "bonus": 0,

            "chipBonus": 0,

            "total": 0,
        }
        for team in team_names
    }

    gw_team_pts = {}

    match_results = []

    for gw in range(
        1,
        current_gw + 1,
    ):

        matchups = fixtures.get(
            gw,
            [],
        )

        # ─────────────────────────────────
        # FPL live player points for this GW
        # ─────────────────────────────────
        live_points = get_gw_live_points(gw)

        # ─────────────────────────────────
        # Raw player points
        # ─────────────────────────────────

        raw_player_pts = {}

        for team in team_names:

            raw_player_pts[team] = (
                team_player_points_dict(
                    team,
                    gw,
                    league,
                    _shared_cache,
                )
            )

        # ─────────────────────────────────
        # Adjusted team points
        # ─────────────────────────────────

        gw_team_pts[gw] = {}

        for team in team_names:

            gw_team_pts[gw][team] = (
                compute_adjusted_team_points(
                    team,
                    gw,
                    raw_player_pts,
                    chips,
                )
            )

        # ─────────────────────────────────
        # Save GW history
        # ─────────────────────────────────

        gw_hist = {}

        for team in team_names:

            gw_hist[team] = {}

            for player_name, entry_id in league[team]["players"].items():

                key = (
                    entry_id,
                    gw,
                )

                pts = _shared_cache.get(key)

                if pts is not None:

                    gw_hist[team][str(entry_id)] = {
                        "player": player_name,
                        "points": pts,
                    }

        gw_hist_dir = (
            REPO_ROOT
            / "data"
            / "gw_history"
        )

        gw_hist_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        gw_hist_file = (
            gw_hist_dir
            / f"gw{gw}.json"
        )

        with open(
            gw_hist_file,
            "w",
            encoding="utf-8",
        ) as file:

            json.dump(
                gw_hist,
                file,
                ensure_ascii=False,
                indent=2,
            )

        # ─────────────────────────────────
        # Save complete FPL lineups
        # ─────────────────────────────────
        gw_lineups = {}

        for team in team_names:
            gw_lineups[team] = {}

            for manager_name, entry_id in league[team]["players"].items():
                lineup = get_player_gw_picks(entry_id, gw, live_points)

                if lineup is None:
                    log.warning(
                        f"GW{gw}: Could not fetch lineup for "
                        f"{team} / {manager_name} (id={entry_id})"
                    )
                    continue

                lineup["manager"] = manager_name
                gw_lineups[team][str(entry_id)] = lineup

        LINEUPS_DIR.mkdir(parents=True, exist_ok=True)
        lineup_file = LINEUPS_DIR / f"gw{gw}.json"

        with open(lineup_file, "w", encoding="utf-8") as file:
            json.dump(
                gw_lineups,
                file,
                ensure_ascii=False,
                indent=2,
            )

        log.info(f"GW{gw}: lineups written → {lineup_file}")

        # ─────────────────────────────────
        # Save complete FPL lineups
        # ─────────────────────────────────
        gw_lineups = {}
        for team in team_names:
            gw_lineups[team] = {}
            for manager_name, entry_id in league[team]["players"].items():
                lineup = get_player_gw_picks(entry_id, gw, live_points)
                if lineup is None:
                    log.warning(
                        f"GW{gw}: Could not fetch lineup for "
                        f"{team} / {manager_name} (id={entry_id})"
                    )
                    continue
                lineup["manager"] = manager_name
                gw_lineups[team][str(entry_id)] = lineup
                time.sleep(API_DELAY)

        LINEUPS_DIR.mkdir(parents=True, exist_ok=True)
        lineup_file = LINEUPS_DIR / f"gw{gw}.json"
        with open(lineup_file, "w", encoding="utf-8") as file:
            json.dump(gw_lineups, file, ensure_ascii=False, indent=2)

        log.info(f"GW{gw}: lineups written → {lineup_file}")

        # ─────────────────────────────────
        # Fixtures
        # ─────────────────────────────────

        log.info(
            f"GW{gw}: Processing "
            f"{len(matchups)} fixture(s)"
        )

        for matchup in matchups:

            if len(matchup) < 2:
                continue

            home, away = (
                matchup[0],
                matchup[1],
            )

            if (
                home == "BYE"
                or away == "BYE"
            ):
                continue

            home_pts = gw_team_pts[gw].get(home)
            away_pts = gw_team_pts[gw].get(away)

            if (
                home_pts is None
                or away_pts is None
            ):

                log.warning(
                    f"GW{gw}: Missing points "
                    f"for {home} vs {away} "
                    f"— skipping"
                )

                continue

            # ─────────────────────────────
            # Played
            # ─────────────────────────────

            stats[home]["played"] += 1
            stats[away]["played"] += 1

            # ─────────────────────────────
            # GF / GA
            # ─────────────────────────────

            stats[home]["gf"] += home_pts
            stats[home]["ga"] += away_pts

            stats[away]["gf"] += away_pts
            stats[away]["ga"] += home_pts

            # ─────────────────────────────
            # 1v1
            # ─────────────────────────────

            duel_result = get_1v1_duel_for_match(
                chips,
                fixtures,
                home,
                away,
                gw,
                raw_player_pts,
            )

            # Create match result
            match_result = build_match_result(
                gw,
                home,
                away,
                home_pts,
                away_pts,
                duel_result,
            )

            match_results.append(
                match_result
            )

            # ─────────────────────────────
            # 1v1 RESULT
            # ─────────────────────────────

            if duel_result:

                duel_team, duel = duel_result

                if duel["myPts"] > duel["oppPts"]:

                    winner = duel_team
                    loser = duel["opponent"]

                    stats[winner]["won"] += 1
                    stats[winner]["matchPts"] += 3
                    stats[loser]["lost"] += 1

                    apply_bonus3_if_won(
                        chips,
                        winner,
                        gw,
                        stats,
                    )

                    log.info(
                        f"GW{gw}: 1v1 WIN → "
                        f"{winner} "
                        f"({duel['myPlayer']} "
                        f"{duel['myPts']}) vs "
                        f"{loser} "
                        f"({duel['oppPlayer']} "
                        f"{duel['oppPts']})"
                    )

                elif duel["oppPts"] > duel["myPts"]:

                    winner = duel["opponent"]
                    loser = duel_team

                    stats[winner]["won"] += 1
                    stats[winner]["matchPts"] += 3
                    stats[loser]["lost"] += 1

                    apply_bonus3_if_won(
                        chips,
                        winner,
                        gw,
                        stats,
                    )

                    log.info(
                        f"GW{gw}: 1v1 LOSS → "
                        f"{loser}; WIN → {winner}"
                    )

                else:

                    stats[home]["draw"] += 1
                    stats[home]["matchPts"] += 1

                    stats[away]["draw"] += 1
                    stats[away]["matchPts"] += 1

                    log.info(
                        f"GW{gw}: 1v1 DRAW → "
                        f"{home} vs {away} "
                        f"({duel['myPts']}-"
                        f"{duel['oppPts']})"
                    )

            # ─────────────────────────────
            # NORMAL RESULT
            # ─────────────────────────────

            elif home_pts > away_pts:

                stats[home]["won"] += 1
                stats[home]["matchPts"] += 3
                stats[away]["lost"] += 1

                apply_bonus3_if_won(
                    chips,
                    home,
                    gw,
                    stats,
                )

                log.info(
                    f"GW{gw}: "
                    f"{home} {home_pts} – "
                    f"{away_pts} {away} "
                    f"→ WIN {home}"
                )

            elif away_pts > home_pts:

                stats[away]["won"] += 1
                stats[away]["matchPts"] += 3
                stats[home]["lost"] += 1

                apply_bonus3_if_won(
                    chips,
                    away,
                    gw,
                    stats,
                )

                log.info(
                    f"GW{gw}: "
                    f"{home} {home_pts} – "
                    f"{away_pts} {away} "
                    f"→ WIN {away}"
                )

            else:

                stats[home]["draw"] += 1
                stats[home]["matchPts"] += 1

                stats[away]["draw"] += 1
                stats[away]["matchPts"] += 1

                log.info(
                    f"GW{gw}: "
                    f"{home} {home_pts} – "
                    f"{away_pts} {away} "
                    f"→ DRAW"
                )

    # ─────────────────────────────────────
    # FINAL TOTALS
    # ─────────────────────────────────────

    for team in team_names:

        stats[team]["gd"] = (
            stats[team]["gf"]
            - stats[team]["ga"]
        )

        stats[team]["total"] = (
            stats[team]["matchPts"]
            + stats[team]["chipBonus"]
        )

    # ─────────────────────────────────────
    # SORT
    # ─────────────────────────────────────

    sorted_teams = sorted(
        team_names,
        key=lambda team: (
            -stats[team]["total"],
            -stats[team]["gd"],
            -stats[team]["matchPts"],
            -stats[team]["gf"],
            -stats[team]["won"],
        ),
    )

    team_rows = [
        {
            "name": team,
            **stats[team],
        }
        for team in sorted_teams
    ]

    return team_rows, match_results


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():

    log.info("═" * 55)

    log.info(
        "El7amla Standings Updater — start"
    )

    log.info("═" * 55)

    # ─────────────────────────────────────
    # 1. Detect current GW
    # ─────────────────────────────────────

    log.info(
        "Fetching current gameweek "
        "from FPL bootstrap…"
    )

    current_gw = get_current_gw()

    log.info(
        f"Current GW: {current_gw}"
    )

    # ─────────────────────────────────────
    # 2. Load local data
    # ─────────────────────────────────────

    league = load_league()
    fixtures = load_fixtures()
    chips = load_chips()

    # ─────────────────────────────────────
    # 3. Respect league season length
    # ─────────────────────────────────────

    season_gws = sorted(
        fixtures
    )

    if not season_gws:

        raise RuntimeError(
            "fixtures.json contains "
            "no gameweeks"
        )

    season_max_gw = season_gws[-1]

    if current_gw > season_max_gw:

        log.info(
            f"Capping FPL GW "
            f"{current_gw} to league "
            f"season GW {season_max_gw}"
        )

        current_gw = season_max_gw

    # ─────────────────────────────────────
    # 4. Shared cache
    # ─────────────────────────────────────

    global _shared_cache, _entry_gw_cache

    _shared_cache = {}
    _entry_gw_cache = {}

    # ─────────────────────────────────────
    # 5. Calculate standings + matches
    # ─────────────────────────────────────

    team_rows, match_results = (
        _run_with_shared_cache(
            current_gw,
            league,
            fixtures,
            chips,
        )
    )

    # ─────────────────────────────────────
    # 6. Player standings
    # ─────────────────────────────────────

    log.info(
        "Calculating player standings…"
    )

    player_rows = (
        calculate_player_standings(
            current_gw,
            fixtures,
            league,
            _shared_cache,
        )
    )

    # ─────────────────────────────────────
    # 7. Write JSON
    # ─────────────────────────────────────

    write_output(
        team_rows,
        player_rows,
        match_results,
        current_gw,
    )

    log.info(
        f"Match results written: "
        f"{len(match_results)}"
    )

    log.info("═" * 55)
    log.info("Done ✓")
    log.info("═" * 55)


# ─────────────────────────────────────────────
# SHARED CACHE
# ─────────────────────────────────────────────

_shared_cache: dict = {}


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    main()
