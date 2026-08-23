# update_standings.py
# ====================
# El7amla 2v2 Fantasy League — Standings Calculator
# Fetches live FPL data and generates current_standings.json

"""
Rules:
  - Player points counted only on gameweeks their team PLAYED (not BYE)
  - Team match points:
        3 = win
        1 = draw
        0 = loss

  - NO regular GW bonus.
    The old +1 bonus for the highest-scoring team has been removed.

Chips System (data/chips.json):
  - one_v_one:
        2x/season
        once per half: GW1-19, GW20-35
        Player vs player duel against the scheduled opponent
        from fixtures.json.

        The duel determines the match winner:
          winner = 3 league points
          loser  = 0 league points
          draw    = 1 league point each

        If both teams activate 1v1 against each other
        in the same fixture, both activations are canceled.

  - bonus3:
        1x/season only.
        If the team WINS its fixture that GW,
        +3 extra league points are added.

  - double_player:
        2x/season
        once per half.

        Chosen player's GW points ×2,
        teammate's points = 0.

        This affects the team's GW total used for:
          - match result
          - GF
          - GA

  - Chip effects only apply to TEAM standings.
  - Individual PLAYER standings remain based on RAW FPL points.

Final Team Total:
    total = matchPts + chipBonus

Run:
    pip install requests
    python update_standings.py
"""

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

FPL_BASE = "https://fantasy.premierleague.com/api"
FPL_BOOTSTRAP = f"{FPL_BASE}/bootstrap-static/"
FPL_PICKS = f"{FPL_BASE}/entry/{{entry_id}}/event/{{gw}}/picks/"

# Polite delay between FPL API calls
API_DELAY = 0.8

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; El7amla-Bot/1.0)",
}


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
            r = requests.get(
                url,
                headers=HEADERS,
                timeout=15,
            )

            if r.status_code == 200:
                return r.json()

            elif r.status_code == 404:
                # GW not played yet / endpoint unavailable
                return None

            else:
                log.warning(
                    f"HTTP {r.status_code} — {url} "
                    f"(attempt {attempt})"
                )

        except requests.RequestException as e:
            log.warning(
                f"Request error — {url} "
                f"(attempt {attempt}): {e}"
            )

        if attempt < retries:
            time.sleep(2 ** attempt)

    return None


def get_current_gw() -> int:
    """
    Return the latest finished gameweek from FPL bootstrap.
    """

    data = fpl_get(FPL_BOOTSTRAP)

    if not data:
        raise RuntimeError(
            "Cannot fetch FPL bootstrap — check connectivity"
        )

    for event in reversed(data["events"]):
        if event["finished"]:
            return event["id"]

    return 1


def get_player_gw_points(
    entry_id: int,
    gw: int,
) -> int | None:
    """
    Return the net points for an FPL entry in a given gameweek.

    Net = active-chip-adjusted points minus transfer cost.

    Returns None if the GW hasn't been played yet.
    """

    url = FPL_PICKS.format(
        entry_id=entry_id,
        gw=gw,
    )

    data = fpl_get(url)

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

    # FPL already returns transfer cost as negative.
    transfer_cost = entry_history.get(
        "event_transfers_cost",
        0,
    )

    net_points = points - transfer_cost

    return net_points


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
    ) as f:
        return json.load(f)["teams"]


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
    ) as f:
        raw = json.load(f)["fixtures"]

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
    ) as f:
        data = json.load(f)

    data.pop("_comment", None)

    return data


# ─────────────────────────────────────────────
# CHIPS SYSTEM — HELPERS
# ─────────────────────────────────────────────

def half_key(gw: int) -> str:
    """
    GW1-19  = first half
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
    Return the activated chip record for:

        team
        chip_key
        exact GW

    Only status == "used" is accepted.
    """

    team_chips = chips.get(team)

    if not team_chips:
        return None

    # bonus3 is a single-use chip
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

    # one_v_one / double_player
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
    Return the team scheduled to play `team`
    in `gw`.
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
    Resolve a valid 1v1 activation for this fixture.

    Rules:
      - If both teams activate 1v1 against each other,
        both activations are canceled.
      - Opponent is determined from fixtures.json.
      - Player points are compared using RAW FPL points.
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

    # Both activated against each other
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

    # Verify fixture
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
            f"for {team} vs {opponent} — "
            f"ignoring activation"
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

    for both players of a team.

    Uses the shared cache to avoid duplicate API calls.
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

        p = cache[key]

        if p is None:
            return None

        result[player_name] = p

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
    Return the team's chip-adjusted total
    points for this GW.

    Only Double Player affects this total.

    This value is used for:
      - match result
      - GF
      - GA

    Bonus is NOT applied here.
    """

    own = raw_player_pts.get(team)

    if own is None:
        return None

    adjusted = dict(own)

    # ── Double Player ──

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
# BONUS3 CHIP
# ─────────────────────────────────────────────

def apply_bonus3_if_won(
    chips: dict,
    team: str,
    gw: int,
    stats: dict,
) -> None:
    """
    Bonus3 chip:

    +3 extra league points if the team
    has an active bonus3 chip AND wins
    its fixture this GW.

    This is NOT the old regular GW bonus.
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
# LEGACY STANDINGS CALCULATION
# ─────────────────────────────────────────────

def calculate_standings(
    current_gw: int,
    chips: dict | None = None,
) -> list[dict]:

    league = load_league()
    fixtures = load_fixtures()

    chips = (
        chips
        if chips is not None
        else load_chips()
    )

    team_names = list(
        league.keys()
    )

    # ── Initialise team stats ──

    stats = {}

    for team in team_names:

        stats[team] = {
            "played": 0,
            "won": 0,
            "draw": 0,
            "lost": 0,

            "gf": 0,
            "ga": 0,
            "gd": 0,

            "matchPts": 0,

            # Old regular bonus removed
            "bonus": 0,

            # Bonus3 only
            "chipBonus": 0,

            "total": 0,
        }

    # ── Cache ──

    pts_cache = {}

    log.info(
        f"Processing GW 1 → {current_gw}"
    )

    # ─────────────────────────────────────
    # GAMEWEEKS
    # ─────────────────────────────────────

    for gw in range(
        1,
        current_gw + 1,
    ):

        matchups = fixtures.get(
            gw,
            [],
        )

        # ── Raw player points ──

        raw_player_pts = {}

        for team in team_names:

            raw_player_pts[team] = (
                team_player_points_dict(
                    team,
                    gw,
                    league,
                    pts_cache,
                )
            )

        # ── Adjusted team points ──

        gw_team_pts = {}

        for team in team_names:

            gw_team_pts[team] = (
                compute_adjusted_team_points(
                    team,
                    gw,
                    raw_player_pts,
                    chips,
                )
            )

        log.info(
            f"GW{gw}: Processing "
            f"{len(matchups)} fixture(s)"
        )

        # ─────────────────────────────────
        # FIXTURES
        # ─────────────────────────────────

        for matchup in matchups:

            home, away = (
                matchup[0],
                matchup[1],
            )

            if (
                home == "BYE"
                or away == "BYE"
            ):
                continue

            home_pts = gw_team_pts.get(
                home
            )

            away_pts = gw_team_pts.get(
                away
            )

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

            # ── Played ──

            stats[home]["played"] += 1
            stats[away]["played"] += 1

            # ── GF / GA ──

            stats[home]["gf"] += home_pts
            stats[home]["ga"] += away_pts

            stats[away]["gf"] += away_pts
            stats[away]["ga"] += home_pts

            # ─────────────────────────────
            # 1v1
            # ─────────────────────────────

            duel_result = (
                get_1v1_duel_for_match(
                    chips,
                    fixtures,
                    home,
                    away,
                    gw,
                    raw_player_pts,
                )
            )

            if duel_result:

                duel_team, duel = (
                    duel_result
                )

                # ── 1v1 WIN ──

                if (
                    duel["myPts"]
                    > duel["oppPts"]
                ):

                    winner = duel_team
                    loser = duel["opponent"]

                    stats[winner]["won"] += 1

                    stats[winner][
                        "matchPts"
                    ] += 3

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

                # ── 1v1 LOSS ──

                elif (
                    duel["oppPts"]
                    > duel["myPts"]
                ):

                    winner = duel["opponent"]
                    loser = duel_team

                    stats[winner]["won"] += 1

                    stats[winner][
                        "matchPts"
                    ] += 3

                    stats[loser]["lost"] += 1

                    apply_bonus3_if_won(
                        chips,
                        winner,
                        gw,
                        stats,
                    )

                    log.info(
                        f"GW{gw}: 1v1 LOSS → "
                        f"{loser}; WIN → "
                        f"{winner}"
                    )

                # ── 1v1 DRAW ──

                else:

                    stats[home]["draw"] += 1
                    stats[home][
                        "matchPts"
                    ] += 1

                    stats[away]["draw"] += 1
                    stats[away][
                        "matchPts"
                    ] += 1

                    log.info(
                        f"GW{gw}: 1v1 DRAW → "
                        f"{home} vs {away} "
                        f"({duel['myPts']}-"
                        f"{duel['oppPts']})"
                    )

            # ─────────────────────────────
            # NORMAL MATCH
            # ─────────────────────────────

            elif home_pts > away_pts:

                stats[home]["won"] += 1

                stats[home][
                    "matchPts"
                ] += 3

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

                stats[away][
                    "matchPts"
                ] += 3

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
                stats[home][
                    "matchPts"
                ] += 1

                stats[away]["draw"] += 1
                stats[away][
                    "matchPts"
                ] += 1

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

        s = stats[team]

        s["gd"] = (
            s["gf"]
            - s["ga"]
        )

        # IMPORTANT:
        # Regular Bonus removed.
        #
        # Total =
        #   Match Points
        #   +
        #   Bonus3 Chip Points

        s["total"] = (
            s["matchPts"]
            + s["chipBonus"]
        )

    # ─────────────────────────────────────
    # SORT
    # ─────────────────────────────────────

    sorted_teams = sorted(
        team_names,
        key=lambda t: (
            -stats[t]["total"],
            -stats[t]["gd"],
            -stats[t]["matchPts"],
            -stats[t]["gf"],
            -stats[t]["won"],
        ),
    )

    return [
        {
            "name": t,
            **stats[t],
        }
        for t in sorted_teams
    ]


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
    Sum each player's RAW points only on
    gameweeks their team actually played.

    Chips do NOT affect player standings.
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

                return (
                    "BYE"
                    in matchup
                )

        return True

    player_totals = []

    for (
        team_name,
        team_data,
    ) in league.items():

        for (
            player_name,
            entry_id,
        ) in team_data["players"].items():

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
    current_gw: int,
) -> None:

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
    }

    with open(
        OUTPUT_FILE,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2,
        )

    log.info(
        f"Written → {OUTPUT_FILE}"
    )


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

    global _shared_cache

    _shared_cache = {}

    # ─────────────────────────────────────
    # 5. Calculate team standings
    # ─────────────────────────────────────

    team_rows = _run_with_shared_cache(
        current_gw,
        league,
        fixtures,
        chips,
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
    # 7. Write output
    # ─────────────────────────────────────

    write_output(
        team_rows,
        player_rows,
        current_gw,
    )

    log.info("═" * 55)
    log.info("Done ✓")
    log.info("═" * 55)


# ─────────────────────────────────────────────
# SHARED CACHE BRIDGE
# ─────────────────────────────────────────────

_shared_cache: dict = {}


def _run_with_shared_cache(
    current_gw: int,
    league: dict,
    fixtures: dict,
    chips: dict,
) -> list:
    """
    Run standings calculation while storing
    all fetched (entry_id, gw) → points
    in _shared_cache.

    Also writes GW history files.
    """

    team_names = list(
        league.keys()
    )

    # ─────────────────────────────────────
    # Initialize stats
    # ─────────────────────────────────────

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

            # Kept in JSON for compatibility,
            # but no longer calculated.
            "bonus": 0,

            "chipBonus": 0,

            "total": 0,
        }
        for team in team_names
    }

    # ─────────────────────────────────────
    # Per-GW adjusted points
    # ─────────────────────────────────────

    gw_team_pts = {}

    # ─────────────────────────────────────
    # Process GWs
    # ─────────────────────────────────────

    for gw in range(
        1,
        current_gw + 1,
    ):

        matchups = fixtures.get(
            gw,
            [],
        )

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

            for (
                player_name,
                entry_id,
            ) in league[team]["players"].items():

                key = (
                    entry_id,
                    gw,
                )

                pts = _shared_cache.get(
                    key
                )

                if pts is not None:

                    gw_hist[team][
                        str(entry_id)
                    ] = {
                        "points": pts
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
        ) as f:

            json.dump(
                gw_hist,
                f,
                ensure_ascii=False,
                indent=2,
            )

        # ─────────────────────────────────
        # Process fixtures
        # ─────────────────────────────────

        log.info(
            f"GW{gw}: Processing fixtures…"
        )

        for matchup in matchups:

            home, away = (
                matchup[0],
                matchup[1],
            )

            if (
                home == "BYE"
                or away == "BYE"
            ):
                continue

            home_pts = (
                gw_team_pts[gw]
                .get(home)
            )

            away_pts = (
                gw_team_pts[gw]
                .get(away)
            )

            if (
                home_pts is None
                or away_pts is None
            ):

                log.warning(
                    f"GW{gw}: Missing pts "
                    f"for {home} vs {away} "
                    f"— skipping"
                )

                continue

            # ─────────────────────────────
            # Played
            # ─────────────────────────────

            stats[home][
                "played"
            ] += 1

            stats[away][
                "played"
            ] += 1

            # ─────────────────────────────
            # GF / GA
            # ─────────────────────────────

            stats[home]["gf"] += (
                home_pts
            )

            stats[home]["ga"] += (
                away_pts
            )

            stats[away]["gf"] += (
                away_pts
            )

            stats[away]["ga"] += (
                home_pts
            )

            # ─────────────────────────────
            # 1v1
            # ─────────────────────────────

            duel_result = (
                get_1v1_duel_for_match(
                    chips,
                    fixtures,
                    home,
                    away,
                    gw,
                    raw_player_pts,
                )
            )

            if duel_result:

                duel_team, duel = (
                    duel_result
                )

                # ── 1v1 WIN ──

                if (
                    duel["myPts"]
                    > duel["oppPts"]
                ):

                    winner = duel_team
                    loser = duel["opponent"]

                    stats[winner][
                        "won"
                    ] += 1

                    stats[winner][
                        "matchPts"
                    ] += 3

                    stats[loser][
                        "lost"
                    ] += 1

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

                # ── 1v1 LOSS ──

                elif (
                    duel["oppPts"]
                    > duel["myPts"]
                ):

                    winner = duel["opponent"]
                    loser = duel_team

                    stats[winner][
                        "won"
                    ] += 1

                    stats[winner][
                        "matchPts"
                    ] += 3

                    stats[loser][
                        "lost"
                    ] += 1

                    apply_bonus3_if_won(
                        chips,
                        winner,
                        gw,
                        stats,
                    )

                    log.info(
                        f"GW{gw}: 1v1 LOSS → "
                        f"{loser}; "
                        f"WIN → {winner}"
                    )

                # ── 1v1 DRAW ──

                else:

                    stats[home][
                        "draw"
                    ] += 1

                    stats[home][
                        "matchPts"
                    ] += 1

                    stats[away][
                        "draw"
                    ] += 1

                    stats[away][
                        "matchPts"
                    ] += 1

                    log.info(
                        f"GW{gw}: 1v1 DRAW → "
                        f"{home} vs {away} "
                        f"({duel['myPts']}-"
                        f"{duel['oppPts']})"
                    )

            # ─────────────────────────────
            # NORMAL MATCH
            # ─────────────────────────────

            elif home_pts > away_pts:

                stats[home][
                    "won"
                ] += 1

                stats[home][
                    "matchPts"
                ] += 3

                stats[away][
                    "lost"
                ] += 1

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

                stats[away][
                    "won"
                ] += 1

                stats[away][
                    "matchPts"
                ] += 3

                stats[home][
                    "lost"
                ] += 1

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

            # ─────────────────────────────
            # DRAW
            # ─────────────────────────────

            else:

                stats[home][
                    "draw"
                ] += 1

                stats[home][
                    "matchPts"
                ] += 1

                stats[away][
                    "draw"
                ] += 1

                stats[away][
                    "matchPts"
                ] += 1

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

        s = stats[team]

        s["gd"] = (
            s["gf"]
            - s["ga"]
        )

        # ─────────────────────────────────
        # NO REGULAR BONUS
        # ─────────────────────────────────
        #
        # Total consists ONLY of:
        #
        #   Match Points
        #   +
        #   Bonus3 Chip Points
        #

        s["total"] = (
            s["matchPts"]
            + s["chipBonus"]
        )

    # ─────────────────────────────────────
    # SORT TEAMS
    # ─────────────────────────────────────

    sorted_teams = sorted(
        team_names,
        key=lambda t: (
            -stats[t]["total"],
            -stats[t]["gd"],
            -stats[t]["matchPts"],
            -stats[t]["gf"],
            -stats[t]["won"],
        ),
    )

    return [
        {
            "name": team,
            **stats[team],
        }
        for team in sorted_teams
    ]


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    main()
