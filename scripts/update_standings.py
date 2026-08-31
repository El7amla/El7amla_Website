# update_standings.py
# ====================
# El7amla 2v2 Fantasy League — Live Standings + Match Results
#
# Reads:
#   league.json
#   fixtures.json
#   data/chips.json (optional)
#
# Writes:
#   data/current_standings.json
#   data/gw_history/gwN.json
#
# Rules:
#   - Player points are counted only for GWs in which their team plays.
#   - Normal match: win=3, draw=1, loss=0.
#   - NO regular highest-score +1 bonus.
#   - bonus3 chip: +3 league points when the team wins that fixture.
#   - double_player affects the team's GW score used for GF/GA/result.
#   - one_v_one compares the selected players and determines the fixture result.
#   - All processed match results are saved in matches_by_gw.

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent

LEAGUE_FILE = REPO_ROOT / "league.json"
FIXTURES_FILE = REPO_ROOT / "fixtures.json"
CHIPS_FILE = REPO_ROOT / "data" / "chips.json"
OUTPUT_FILE = REPO_ROOT / "data" / "current_standings.json"
HISTORY_DIR = REPO_ROOT / "data" / "gw_history"

FPL_BASE = "https://fantasy.premierleague.com/api"
FPL_BOOTSTRAP = f"{FPL_BASE}/bootstrap-static/"
FPL_PICKS = f"{FPL_BASE}/entry/{{entry_id}}/event/{{gw}}/picks/"

API_DELAY = 0.8
REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; El7amla-Bot/1.0)"
}


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger(__name__)


# ============================================================
# FPL API
# ============================================================

def fpl_get(url: str, retries: int = 3) -> dict | None:
    """GET an FPL endpoint with retries."""
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(
                url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 200:
                return response.json()

            if response.status_code == 404:
                log.warning("HTTP 404 — %s", url)
                return None

            log.warning(
                "HTTP %s — %s (attempt %s/%s)",
                response.status_code,
                url,
                attempt,
                retries,
            )

        except requests.RequestException as exc:
            log.warning(
                "Request error — %s (attempt %s/%s): %s",
                url,
                attempt,
                retries,
                exc,
            )

        if attempt < retries:
            time.sleep(2 ** attempt)

    return None


def get_current_gw() -> int:
    """Return the latest finished FPL gameweek."""
    data = fpl_get(FPL_BOOTSTRAP)
    if not data:
        raise RuntimeError("Cannot fetch FPL bootstrap — check connectivity")

    events = data.get("events", [])

    for event in reversed(events):
        if event.get("finished") is True:
            return int(event["id"])

    return 1


def get_player_gw_points(entry_id: int, gw: int) -> int | None:
    """Return net FPL points for one entry in one GW."""
    url = FPL_PICKS.format(entry_id=entry_id, gw=gw)
    data = fpl_get(url)

    if data is None:
        return None

    history = data.get("entry_history", {})
    points = int(history.get("points", 0) or 0)
    transfer_cost = int(history.get("event_transfers_cost", 0) or 0)

    # FPL exposes event_transfers_cost as a positive cost value.
    return points - transfer_cost


# ============================================================
# LOAD DATA
# ============================================================

def load_json(path: Path, default=None):
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(path)

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_league() -> dict:
    data = load_json(LEAGUE_FILE)
    return data["teams"]


def load_fixtures() -> dict[int, list]:
    data = load_json(FIXTURES_FILE)
    raw = data.get("fixtures", {})
    return {int(gw): matchups for gw, matchups in raw.items()}


def load_chips() -> dict:
    if not CHIPS_FILE.exists():
        log.info("chips.json not found — running without chip adjustments")
        return {}

    data = load_json(CHIPS_FILE, {})
    if isinstance(data, dict):
        data.pop("_comment", None)
    return data


# ============================================================
# CHIPS
# ============================================================

def half_key(gw: int) -> str:
    return "h1" if gw <= 19 else "h2"


def get_chip_slot(
    chips: dict,
    team: str,
    chip_key: str,
    gw: int,
) -> dict | None:
    team_chips = chips.get(team) or {}

    if chip_key == "bonus3":
        slot = team_chips.get("bonus3")
        if (
            slot
            and slot.get("used")
            and int(slot.get("gw", -1)) == gw
            and slot.get("status") == "used"
        ):
            return slot
        return None

    slot = (team_chips.get(chip_key) or {}).get(half_key(gw))

    if (
        slot
        and int(slot.get("gw", -1)) == gw
        and slot.get("status") == "used"
    ):
        return slot

    return None


# ============================================================
# FIXTURE / 1v1
# ============================================================

def get_scheduled_opponent(fixtures: dict, team: str, gw: int) -> str | None:
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
    """Return the active 1v1 duel for this fixture, if valid."""

    home_duel = get_chip_slot(chips, home, "one_v_one", gw)
    away_duel = get_chip_slot(chips, away, "one_v_one", gw)

    # Mutual activation cancels both.
    if home_duel and away_duel:
        log.info(
            "GW%s: 1v1 mutual activation %s vs %s — both canceled",
            gw,
            home,
            away,
        )
        return None

    if home_duel:
        duel_team = home
        opponent = away
        duel = home_duel
    elif away_duel:
        duel_team = away
        opponent = home
        duel = away_duel
    else:
        return None

    # Never allow a 1v1 against a non-scheduled opponent.
    if get_scheduled_opponent(fixtures, duel_team, gw) != opponent:
        log.warning(
            "GW%s: 1v1 fixture mismatch for %s — ignoring activation",
            gw,
            duel_team,
        )
        return None

    my_player = duel.get("myPlayer")
    opp_player = duel.get("oppPlayer")

    my_pts = (raw_player_pts.get(duel_team) or {}).get(my_player)
    opp_pts = (raw_player_pts.get(opponent) or {}).get(opp_player)

    if my_pts is None or opp_pts is None:
        log.warning(
            "GW%s: missing 1v1 player data for %s vs %s — ignoring activation",
            gw,
            duel_team,
            opponent,
        )
        return None

    return duel_team, {
        "opponent": opponent,
        "myPlayer": my_player,
        "oppPlayer": opp_player,
        "myPts": my_pts,
        "oppPts": opp_pts,
    }


# ============================================================
# POINTS / CACHE
# ============================================================

def team_player_points_dict(
    team_name: str,
    gw: int,
    league: dict,
    cache: dict,
) -> dict | None:
    """Return {player_name: net_points}; None if data is unavailable."""

    players = league[team_name]["players"]
    result = {}

    for player_name, entry_id in players.items():
        key = (int(entry_id), gw)

        if key not in cache:
            log.info(
                "Fetching GW%s — %s / %s (id=%s)",
                gw,
                team_name,
                player_name,
                entry_id,
            )
            cache[key] = get_player_gw_points(int(entry_id), gw)
            time.sleep(API_DELAY)

        points = cache[key]

        if points is None:
            return None

        result[player_name] = points

    return result


def compute_adjusted_team_points(
    team: str,
    gw: int,
    raw_player_pts: dict,
    chips: dict,
) -> int | None:
    """Apply only double_player to the team's GW score."""

    own = raw_player_pts.get(team)
    if own is None:
        return None

    adjusted = dict(own)
    dbl = get_chip_slot(chips, team, "double_player", gw)

    if dbl:
        doubled = dbl.get("doubledPlayer")
        zeroed = dbl.get("zeroedPlayer")

        if doubled in adjusted:
            adjusted[doubled] *= 2
        if zeroed in adjusted:
            adjusted[zeroed] = 0

        log.info(
            "GW%s: %s double_player → %s ×2, %s = 0",
            gw,
            team,
            doubled,
            zeroed,
        )

    return sum(adjusted.values())


def apply_bonus3_if_won(chips: dict, team: str, gw: int, stats: dict) -> None:
    slot = get_chip_slot(chips, team, "bonus3", gw)
    if slot:
        stats[team]["chipBonus"] += 3
        log.info("GW%s: Bonus3 → %s +3 league points", gw, team)


# ============================================================
# MATCH RESULT
# ============================================================

def build_match_result(
    gw: int,
    home: str,
    away: str,
    home_pts: int,
    away_pts: int,
    duel_result=None,
) -> dict:
    """Create one JSON-safe match object."""

    winner = None

    if duel_result:
        duel_team, duel = duel_result

        if duel["myPts"] > duel["oppPts"]:
            winner = duel_team
        elif duel["oppPts"] > duel["myPts"]:
            winner = duel["opponent"]

        if winner == home:
            result_type = "home_win"
        elif winner == away:
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


# ============================================================
# MAIN CALCULATION
# ============================================================

def calculate_all(
    current_gw: int,
    league: dict,
    fixtures: dict,
    chips: dict,
    cache: dict,
):
    """
    Calculate standings and all completed match results from GW1 to current_gw.
    Returns: team_rows, matches_by_gw
    """

    team_names = list(league.keys())

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
            "bonus": 0,      # kept for compatibility; regular bonus removed
            "chipBonus": 0,
            "total": 0,
        }
        for team in team_names
    }

    matches_by_gw: dict[str, list] = {}

    for gw in range(1, current_gw + 1):
        matchups = fixtures.get(gw, [])
        matches_by_gw[str(gw)] = []

        log.info("────────────────────────────────────────")
        log.info("GW%s: processing %s fixture(s)", gw, len(matchups))

        # --------------------------------------------------------
        # Raw player points
        # --------------------------------------------------------
        raw_player_pts = {}
        for team in team_names:
            raw_player_pts[team] = team_player_points_dict(
                team,
                gw,
                league,
                cache,
            )

        # --------------------------------------------------------
        # Team totals after Double Player
        # --------------------------------------------------------
        gw_team_pts = {}
        for team in team_names:
            gw_team_pts[team] = compute_adjusted_team_points(
                team,
                gw,
                raw_player_pts,
                chips,
            )

        # --------------------------------------------------------
        # Save GW history
        # --------------------------------------------------------
        gw_hist = {}
        for team in team_names:
            gw_hist[team] = {}
            for player_name, entry_id in league[team]["players"].items():
                pts = cache.get((int(entry_id), gw))
                if pts is not None:
                    gw_hist[team][str(entry_id)] = {
                        "player": player_name,
                        "points": pts,
                    }

        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        history_file = HISTORY_DIR / f"gw{gw}.json"
        with history_file.open("w", encoding="utf-8") as f:
            json.dump(gw_hist, f, ensure_ascii=False, indent=2)

        # --------------------------------------------------------
        # Fixtures + match results
        # --------------------------------------------------------
        for matchup in matchups:
            if len(matchup) < 2:
                continue

            home, away = matchup[0], matchup[1]

            if home == "BYE" or away == "BYE":
                continue

            home_pts = gw_team_pts.get(home)
            away_pts = gw_team_pts.get(away)

            # Critical behavior:
            # Do not write a fake 0-0 result if FPL data is unavailable.
            # The fixture stays absent from matches_by_gw until data exists.
            if home_pts is None or away_pts is None:
                log.warning(
                    "GW%s: Missing points for %s vs %s — match not recorded",
                    gw,
                    home,
                    away,
                )
                continue

            home_pts = int(home_pts)
            away_pts = int(away_pts)

            stats[home]["played"] += 1
            stats[away]["played"] += 1

            stats[home]["gf"] += home_pts
            stats[home]["ga"] += away_pts
            stats[away]["gf"] += away_pts
            stats[away]["ga"] += home_pts

            duel_result = get_1v1_duel_for_match(
                chips,
                fixtures,
                home,
                away,
                gw,
                raw_player_pts,
            )

            # ----------------------------------------------------
            # Determine league match result
            # ----------------------------------------------------
            if duel_result:
                duel_team, duel = duel_result

                if duel["myPts"] > duel["oppPts"]:
                    winner = duel_team
                    loser = duel["opponent"]
                    stats[winner]["won"] += 1
                    stats[winner]["matchPts"] += 3
                    stats[loser]["lost"] += 1
                    apply_bonus3_if_won(chips, winner, gw, stats)

                elif duel["oppPts"] > duel["myPts"]:
                    winner = duel["opponent"]
                    loser = duel_team
                    stats[winner]["won"] += 1
                    stats[winner]["matchPts"] += 3
                    stats[loser]["lost"] += 1
                    apply_bonus3_if_won(chips, winner, gw, stats)

                else:
                    stats[home]["draw"] += 1
                    stats[away]["draw"] += 1
                    stats[home]["matchPts"] += 1
                    stats[away]["matchPts"] += 1

            elif home_pts > away_pts:
                stats[home]["won"] += 1
                stats[home]["matchPts"] += 3
                stats[away]["lost"] += 1
                apply_bonus3_if_won(chips, home, gw, stats)

            elif away_pts > home_pts:
                stats[away]["won"] += 1
                stats[away]["matchPts"] += 3
                stats[home]["lost"] += 1
                apply_bonus3_if_won(chips, away, gw, stats)

            else:
                stats[home]["draw"] += 1
                stats[away]["draw"] += 1
                stats[home]["matchPts"] += 1
                stats[away]["matchPts"] += 1

            match = build_match_result(
                gw,
                home,
                away,
                home_pts,
                away_pts,
                duel_result,
            )
            matches_by_gw[str(gw)].append(match)

            log.info(
                "GW%s: %s %s - %s %s (%s)",
                gw,
                home,
                home_pts,
                away_pts,
                away,
                match["result"],
            )

    # --------------------------------------------------------
    # Final totals
    # --------------------------------------------------------
    for team in team_names:
        stats[team]["gd"] = stats[team]["gf"] - stats[team]["ga"]
        stats[team]["total"] = (
            stats[team]["matchPts"]
            + stats[team]["chipBonus"]
        )

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

    return team_rows, matches_by_gw


# ============================================================
# PLAYER STANDINGS
# ============================================================

def calculate_player_standings(
    current_gw: int,
    fixtures: dict,
    league: dict,
    cache: dict,
) -> list[dict]:
    """Sum RAW player points only for GWs in which their team played."""
    
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

                pts = cache.get((int(entry_id), gw))
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
        key=lambda player: -player["points"],
    )


# ============================================================
# OUTPUT
# ============================================================

def write_output(
    team_rows: list,
    player_rows: list,
    matches_by_gw: dict,
    current_gw: int,
) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    # Flat list is kept for compatibility with older pages.
    matches = []
    for gw in sorted(matches_by_gw, key=int):
        matches.extend(matches_by_gw[gw])

    payload = {
        "current_gw": current_gw,
        "last_updated": now,
        "teams": team_rows,
        "players": player_rows,
        "matches_by_gw": matches_by_gw,
        "matches": matches,
    }

    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2,
        )

    log.info(
        "Written → %s | GW=%s | matches=%s",
        OUTPUT_FILE,
        current_gw,
        len(matches),
    )


# ============================================================
# MAIN
# ============================================================

def main():
    log.info("═" * 60)
    log.info("El7amla Standings Updater — start")
    log.info("═" * 60)

    current_gw = get_current_gw()
    log.info("FPL latest finished GW: %s", current_gw)

    league = load_league()
    fixtures = load_fixtures()
    chips = load_chips()

    if not fixtures:
        raise RuntimeError("fixtures.json contains no gameweeks")

    season_max_gw = max(fixtures)

    if current_gw > season_max_gw:
        log.info(
            "Capping FPL GW %s to league GW %s",
            current_gw,
            season_max_gw,
        )
        current_gw = season_max_gw

    # One cache is shared by team standings, match results,
    # player standings, and GW history.
    cache = {}

    team_rows, matches_by_gw = calculate_all(
        current_gw,
        league,
        fixtures,
        chips,
        cache,
    )

    player_rows = calculate_player_standings(
        current_gw,
        fixtures,
        league,
        cache,
    )

    write_output(
        team_rows,
        player_rows,
        matches_by_gw,
        current_gw,
    )

    log.info("═" * 60)
    log.info("Done ✓")
    log.info("═" * 60)


if __name__ == "__main__":
    main()
