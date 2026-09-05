# update_standings.py
# ====================
# El7amla 2v2 Fantasy League — Standings Calculator
# Fetches FPL data and generates current_standings.json
#
# UPDATED (this revision):
#   - get_current_gw() no longer waits for FPL to mark a gameweek
#     "finished". It now returns the highest GW whose deadline has
#     already passed, so a gameweek that is currently being played
#     (deadline passed, matches live, bonus points not final yet) is
#     picked up immediately instead of being invisible until FPL
#     flips finished=true — often days later.
#   - team_player_points_dict() no longer returns None (and silently
#     drops) the WHOLE team the moment a single player's FPL data is
#     unreachable. It now returns (points_dict, missing_list) so a
#     partial team total is always available, and the caller can
#     surface exactly which player/GW failed instead of losing the
#     entire fixture. This is the root-cause fix for the GW1 bug
#     where Shika's unreachable legacy FPL id (5145412) silently
#     poisoned and dropped the Royal Authority vs The Masterminds
#     fixture, Shika's own lineup, and cascaded into a missed
#     transfer-cost deduction in GW2.
#   - build_match_result() accepts an `incomplete` dict of
#     { team: [missing_player_names] } and, when present, marks the
#     match "type": "incomplete" (or keeps "1v1" but still attaches
#     "missingPlayers") instead of pretending the score is final.
#   - data_warnings is now actually collected end-to-end and written
#     into current_standings.json (previously only documented in
#     docstrings, never wired into the output payload).
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
PLAYER_TRANSFERS_FILE = REPO_ROOT / "data" / "player_transfers.json"
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
    """
    Return the gameweek that should currently be displayed/processed.

    FIXED: this previously only returned a GW once FPL marked it
    "finished", which meant a gameweek that had already started
    (deadline passed, matches being played live) was completely
    invisible to this script the entire time it was in progress — the
    site kept showing the PREVIOUS gameweek as "current" until FPL
    flipped finished=true, sometimes days after the round actually
    began.

    New behaviour: a GW counts as "current" once its deadline has
    passed, regardless of whether FPL has finished processing bonus
    points or marked it finished. We take the highest GW id whose
    deadline_time is already in the past. If no GW has started yet
    (e.g. right before the season begins), we default to GW1 so the
    rest of the pipeline still has something sensible to work with.
    """
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

    now = datetime.now(timezone.utc)
    started_gws = []

    for event in data.get("events", []):
        deadline = event.get("deadline_time")
        event_id = event.get("id")

        if not deadline or event_id is None:
            continue

        try:
            deadline_dt = datetime.fromisoformat(
                deadline.replace("Z", "+00:00")
            )
        except ValueError:
            continue

        if deadline_dt <= now:
            started_gws.append(event_id)

    if started_gws:
        current = max(started_gws)
        log.info(
            f"Gameweek {current}'s deadline has passed — "
            f"treating it as the current gameweek to process "
            f"(regardless of FPL's 'finished' flag)"
        )
        return current

    log.info("No gameweek deadline has passed yet — defaulting to GW1")
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


def load_player_transfers() -> dict:
    """
    Load data/player_transfers.json.

    Structure (FPL account migration — SAME person, different FPL
    entry id mid-season; NOT a different player joining the team):
        {
          "TeamName": {
            "changes": [
              {
                "person": "Manager Name",       # optional, for docs only
                "effective_gw": 2,
                "old_entry_id": 111,
                "new_entry_id": 222,
                "free_transfers": 1,
                "transfers_made": 7,
                "cost_per_transfer": 4
              }
            ]
          }
        }

    Returns {} if the file doesn't exist (feature is fully optional —
    teams with no entry behave exactly as before).
    """

    if not PLAYER_TRANSFERS_FILE.exists():
        log.info(
            "player_transfers.json not found — "
            "running without mid-season FPL account-migration adjustments"
        )
        return {}

    with open(PLAYER_TRANSFERS_FILE, encoding="utf-8") as file:
        data = json.load(file)

    data.pop("_comment", None)

    return data


# ─────────────────────────────────────────────
# PLAYER TRANSFERS SYSTEM
# ─────────────────────────────────────────────

def _find_change_for_current_id(
    team: str,
    current_id: int,
    transfers: dict,
) -> dict | None:
    """
    Find the account-migration record (if any) whose new_entry_id matches
    the id currently stored in league.json for this team/slot.
    """

    team_transfers = transfers.get(team)

    if not team_transfers:
        return None

    for change in team_transfers.get("changes", []):

        try:
            new_id = int(change.get("new_entry_id"))
        except (TypeError, ValueError):
            continue

        if new_id == int(current_id):
            return change

    return None


def resolve_player_id_for_gw(
    team: str,
    current_id: int,
    gw: int,
    transfers: dict,
) -> int | None:
    """
    Resolve the FPL entry id to actually use for this team's player slot
    (identified by the id CURRENTLY stored in league.json) at a given GW.

    This is the SAME real person before and after — only the FPL entry
    id changes (account migration), never their identity/name.

    Before the change's effective_gw → old_entry_id (real historical data,
    never overwritten).
    From effective_gw onward → current_id (== new_entry_id) unchanged.

    Returns None if the slot's old id is unknown for a GW before the swap
    (caller must skip fetching rather than invent an id).
    """

    change = _find_change_for_current_id(team, current_id, transfers)

    if not change:
        return current_id

    effective_gw = int(change.get("effective_gw", 1))

    if gw < effective_gw:
        old_id = change.get("old_entry_id")
        return int(old_id) if old_id is not None else None

    return current_id


def resolve_player_name_for_gw(
    team: str,
    current_name: str,
    current_id: int,
    gw: int,
    transfers: dict,
) -> str:
    """
    The person's display name NEVER changes across an FPL account
    migration — only the underlying entry id does. This function is
    kept (rather than removed) so every call site that historically
    asked "what name should this GW show" keeps working unchanged, but
    it now always returns the same league.json name — we never rename
    someone to "(اللاعب السابق)" or similar, because it's the same
    person, not a different one.
    """

    return current_name


def get_transfer_cost_for_gw(
    team: str,
    gw: int,
    transfers: dict,
) -> int:
    """
    Sum of all transfer-cost penalties that apply to `team` on exactly
    this `gw` (i.e. change["effective_gw"] == gw). Zero for every other
    GW, so the -24 (or whatever) is never repeated.

        charged_transfers = max(0, transfers_made - free_transfers)
        transfer_cost = charged_transfers * cost_per_transfer
    """

    team_transfers = transfers.get(team)

    if not team_transfers:
        return 0

    total_cost = 0

    for change in team_transfers.get("changes", []):

        if int(change.get("effective_gw", -1)) != int(gw):
            continue

        made = int(change.get("transfers_made", 0) or 0)
        free = int(change.get("free_transfers", 0) or 0)
        cost_each = int(change.get("cost_per_transfer", 4) or 4)

        charged = max(0, made - free)
        total_cost += charged * cost_each

    return total_cost


def get_transfer_details_for_gw(
    team: str,
    gw: int,
    transfers: dict,
) -> dict | None:
    """
    Same as get_transfer_cost_for_gw but returns the full breakdown
    (used to embed transparency info into current_standings.json).
    Returns None if nothing applies to this team/gw.
    """

    team_transfers = transfers.get(team)

    if not team_transfers:
        return None

    for change in team_transfers.get("changes", []):

        if int(change.get("effective_gw", -1)) != int(gw):
            continue

        made = int(change.get("transfers_made", 0) or 0)
        free = int(change.get("free_transfers", 0) or 0)
        cost_each = int(change.get("cost_per_transfer", 4) or 4)
        charged = max(0, made - free)

        return {
            "gw": gw,
            "team": team,
            "transfers_made": made,
            "free_transfers": free,
            "charged_transfers": charged,
            "cost_per_transfer": cost_each,
            "transfer_cost": charged * cost_each,
        }

    return None


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
    transfers: dict,
    warnings: list | None = None,
) -> tuple[dict, list]:
    """
    Return:

        (
            { player_name: raw_points, ... },  # players we DID fetch
            [ missing_player_name, ... ],       # players we could NOT fetch
        )

    Uses shared cache. Resolves each player's FPL entry id per-GW via
    player_transfers.json, so a mid-season replacement never overwrites
    the departing manager's real GW history, and never repeats itself
    once the new manager is in place.

    FIXED: this used to return None for the ENTIRE team the moment a
    single player's data was unreachable, which caused the match loop
    to silently `continue` and drop the whole fixture from `matches`
    (this is exactly what happened with Shika's unreachable legacy FPL
    id 5145412 in GW1 — it poisoned and hid the entire Royal Authority
    vs The Masterminds result).

    Now it ALWAYS returns whatever real points it could fetch for every
    player it could reach, and separately reports which players it
    could not — so the caller can compute a partial-but-real team total
    and flag the gap (via `missing`/`warnings`/`data_warnings` and the
    match's `missingPlayers` field) instead of losing the whole
    gameweek. We never fabricate a 0 for a missing player — we simply
    don't include them in the sum, and make the omission visible.
    """

    players = league[team_name]["players"]

    result = {}
    missing = []

    for player_name, current_id in players.items():

        entry_id = resolve_player_id_for_gw(
            team_name,
            current_id,
            gw,
            transfers,
        )

        if entry_id is None:
            # A transfer is configured for this slot but the OLD id
            # hasn't been supplied yet — do not fabricate data.
            msg = (
                f"GW{gw}: {team_name} / {player_name} — "
                f"old_player_id not set in player_transfers.json"
            )
            log.warning(msg)
            if warnings is not None:
                warnings.append(msg)
            missing.append(player_name)
            continue

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
            msg = (
                f"GW{gw}: {team_name} / {player_name} (id={entry_id}) — "
                f"FPL API returned no data for this entry/GW "
                f"(404, private entry, or the id does not exist for that GW). "
                f"Check https://fantasy.premierleague.com/api/entry/{entry_id}/event/{gw}/picks/ "
                f"directly to confirm the id is correct."
            )
            log.warning(msg)
            if warnings is not None:
                warnings.append(msg)
            missing.append(player_name)
            continue

        result[player_name] = points

    if missing:
        log.warning(
            f"GW{gw}: {team_name} — partial data only "
            f"({len(result)} fetched, {len(missing)} missing: "
            f"{', '.join(missing)})"
        )

    return result, missing


# ─────────────────────────────────────────────
# TEAM POINT CALCULATION
# ─────────────────────────────────────────────

def compute_adjusted_team_points(
    team: str,
    gw: int,
    raw_player_pts: dict,
    chips: dict,
    transfers: dict,
) -> int | None:
    """
    Double Player affects team GW points.
    Transfer cost (from player_transfers.json) is subtracted here too,
    so it flows into match results / GF / GA / standings — not just
    display — and only on the exact GW it applies to.

    NOTE: `raw_player_pts[team]` is now always a dict (possibly
    partial, possibly empty — never None) thanks to the fix in
    team_player_points_dict(). The `own is None` guard below is kept
    purely as defensive programming in case this function is ever
    called with a team key that was never populated at all.

    Used for:
      - match result
      - GF
      - GA
      - standings total
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

    total = sum(adjusted.values())

    transfer_cost = get_transfer_cost_for_gw(team, gw, transfers)

    if transfer_cost:
        log.info(
            f"GW{gw}: {team} transfer cost applied → -{transfer_cost} "
            f"(raw {total} → final {total - transfer_cost})"
        )
        total -= transfer_cost

    return total


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
    incomplete: dict | None = None,
) -> dict:
    """
    Build a JSON-safe match result.

    This is used by standings.html to display
    actual scores instead of only fixtures.

    NEW: `incomplete`, if provided, is
        { team_name: [missing_player_name, ...] }
    for any team in this fixture that had one or more players whose
    real FPL points could not be fetched this GW. When present:
      - a "normal" match is marked "type": "incomplete" instead of
        "normal" (the score is still whatever partial points we did
        collect — never a fabricated full score)
      - a "1v1" match keeps "type": "1v1" (the duel itself is only
        ever built from players we DID successfully fetch) but still
        carries "missingPlayers" so the wider team score is flagged
      Either way, "missingPlayers" is attached so the frontend/admin
      tooling can show a clear "data incomplete" indicator instead of
      silently presenting a partial score as final.
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

        result_dict = {
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

        if incomplete:
            result_dict["missingPlayers"] = incomplete

        return result_dict

    if home_pts > away_pts:

        winner = home
        result_type = "home_win"

    elif away_pts > home_pts:

        winner = away
        result_type = "away_win"

    else:

        result_type = "draw"

    result_dict = {
        "gw": gw,
        "home": home,
        "away": away,
        "homePts": home_pts,
        "awayPts": away_pts,
        "result": result_type,
        "winner": winner,
        "type": "incomplete" if incomplete else "normal",
    }

    if incomplete:
        result_dict["missingPlayers"] = incomplete

    return result_dict


# ─────────────────────────────────────────────
# PLAYER STANDINGS
# ─────────────────────────────────────────────

def calculate_player_standings(
    current_gw: int,
    fixtures: dict,
    league: dict,
    pts_cache: dict,
    transfers: dict,
) -> list[dict]:
    """
    Sum raw player points only for GWs where the player's team actually
    played. When an FPL account migration is configured for a slot
    (data/player_transfers.json), the points from BOTH the old and new
    entry id are summed under ONE row — it's the same real person, just
    a different underlying FPL account per GW, never two separate rows.
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

        for player_name, current_id in team_data["players"].items():

            total = 0

            for gw in range(1, current_gw + 1):

                if team_has_bye(team_name, gw):
                    continue

                # Resolves to old_entry_id before the migration's
                # effective_gw and to current_id (new_entry_id) from
                # then on — same person, correct account per GW.
                resolved_id = resolve_player_id_for_gw(
                    team_name, current_id, gw, transfers
                )

                if resolved_id is None:
                    continue

                pts = pts_cache.get((resolved_id, gw))

                if pts is not None:
                    total += pts

            player_totals.append({
                "name": player_name,
                "team": team_name,
                "points": total,
            })

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
    transfer_adjustments: list,
    data_warnings: list,
    current_gw: int,
) -> None:
    """
    Write current_standings.json.

      matches = all processed match results (may include
      "type": "incomplete" entries — see build_match_result).

      transfer_adjustments = transparency log of every transfer-cost
      deduction actually applied (team, gw, breakdown), so nothing is
      silently hidden inside the totals.

      data_warnings = every "couldn't fetch this player/GW" warning
      collected during the run (previously only logged to the CI
      console — now actually shipped in the JSON so anyone looking at
      current_standings.json can see exactly what's incomplete and
      why, without digging through GitHub Actions logs).
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

        "matches": matches,

        "transfer_adjustments": transfer_adjustments,

        "data_warnings": data_warnings,
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
    transfers: dict,
) -> tuple[list, list, list, list]:
    """
    Calculate team standings and match results.

    Returns:
        (
            team_rows,
            match_results,
            transfer_adjustments,
            data_warnings,
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
    transfer_adjustments = []
    data_warnings: list = []

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
        # Record any transfer-cost adjustment
        # that applies to this GW (transparency log)
        # ─────────────────────────────────

        for team in team_names:
            details = get_transfer_details_for_gw(team, gw, transfers)
            if details:
                transfer_adjustments.append(details)

        # ─────────────────────────────────
        # Raw player points
        # (now always a dict per team — never None — plus a parallel
        # missing_by_team map of anyone we couldn't fetch this GW)
        # ─────────────────────────────────

        raw_player_pts = {}
        missing_by_team = {}

        for team in team_names:

            pts, missing = team_player_points_dict(
                team,
                gw,
                league,
                _shared_cache,
                transfers,
                warnings=data_warnings,
            )

            raw_player_pts[team] = pts
            missing_by_team[team] = missing

        # ─────────────────────────────────
        # Adjusted team points
        # (double_player chip + transfer cost)
        # ─────────────────────────────────

        gw_team_pts[gw] = {}

        for team in team_names:

            gw_team_pts[gw][team] = (
                compute_adjusted_team_points(
                    team,
                    gw,
                    raw_player_pts,
                    chips,
                    transfers,
                )
            )

        # ─────────────────────────────────
        # Save GW history
        # (uses resolved id + resolved name so a replaced
        # manager's real GW1 data is preserved under their
        # OWN id, not silently merged into the new manager)
        # ─────────────────────────────────

        gw_hist = {}

        for team in team_names:

            gw_hist[team] = {}

            for player_name, current_id in league[team]["players"].items():

                resolved_id = resolve_player_id_for_gw(
                    team, current_id, gw, transfers
                )

                if resolved_id is None:
                    continue

                resolved_name = resolve_player_name_for_gw(
                    team, player_name, current_id, gw, transfers
                )

                key = (
                    resolved_id,
                    gw,
                )

                pts = _shared_cache.get(key)

                if pts is not None:

                    gw_hist[team][str(resolved_id)] = {
                        "player": resolved_name,
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
        # (single pass — resolved id/name per GW)
        # ─────────────────────────────────

        gw_lineups = {}

        for team in team_names:

            gw_lineups[team] = {}

            for manager_name, current_id in league[team]["players"].items():

                resolved_id = resolve_player_id_for_gw(
                    team, current_id, gw, transfers
                )

                if resolved_id is None:
                    log.warning(
                        f"GW{gw}: skipping lineup for {team} / "
                        f"{manager_name} — old_player_id not configured "
                        f"in player_transfers.json"
                    )
                    continue

                resolved_name = resolve_player_name_for_gw(
                    team, manager_name, current_id, gw, transfers
                )

                lineup = get_player_gw_picks(resolved_id, gw, live_points)

                if lineup is None:
                    log.warning(
                        f"GW{gw}: Could not fetch lineup for "
                        f"{team} / {resolved_name} (id={resolved_id})"
                    )
                    continue

                lineup["manager"] = resolved_name
                gw_lineups[team][str(resolved_id)] = lineup

                time.sleep(API_DELAY)

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

                # This should now only happen if a team key was never
                # populated at all (e.g. missing from league.json for
                # this fixture) — a real structural problem, not a
                # single unreachable FPL account, which no longer
                # causes this branch to fire.
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

            # ─────────────────────────────
            # Data-completeness flag for
            # this specific fixture
            # ─────────────────────────────

            incomplete = {}

            if missing_by_team.get(home):
                incomplete[home] = missing_by_team[home]

            if missing_by_team.get(away):
                incomplete[away] = missing_by_team[away]

            # Create match result
            match_result = build_match_result(
                gw,
                home,
                away,
                home_pts,
                away_pts,
                duel_result,
                incomplete=incomplete or None,
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

            if incomplete:
                log.warning(
                    f"GW{gw}: {home} vs {away} recorded with "
                    f"PARTIAL data — missing: {incomplete}"
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

    return team_rows, match_results, transfer_adjustments, data_warnings


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
    transfers = load_player_transfers()

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

    team_rows, match_results, transfer_adjustments, data_warnings = (
        _run_with_shared_cache(
            current_gw,
            league,
            fixtures,
            chips,
            transfers,
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
            transfers,
        )
    )

    # ─────────────────────────────────────
    # 7. Write JSON
    # ─────────────────────────────────────

    write_output(
        team_rows,
        player_rows,
        match_results,
        transfer_adjustments,
        data_warnings,
        current_gw,
    )

    log.info(
        f"Match results written: "
        f"{len(match_results)}"
    )

    if transfer_adjustments:
        log.info(
            f"Transfer-cost adjustments applied: "
            f"{len(transfer_adjustments)}"
        )
        for adj in transfer_adjustments:
            log.info(
                f"  GW{adj['gw']} {adj['team']}: "
                f"{adj['transfers_made']} made, "
                f"{adj['free_transfers']} free, "
                f"{adj['charged_transfers']} charged "
                f"→ -{adj['transfer_cost']}"
            )

    if data_warnings:
        log.warning(
            f"Data warnings recorded this run: "
            f"{len(data_warnings)} "
            f"(also written into current_standings.json.data_warnings)"
        )
        for w in data_warnings:
            log.warning(f"  {w}")
    else:
        log.info("No data warnings — all player data fetched cleanly.")

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
