import json
import requests
from datetime import datetime

# ==============================================
#  بيانات الفرق واللاعبين (ثابتة)
# ==============================================

TEAMS = {
    "Black Tech": {
        "players": {
            "Elfiky": 7002106,
            "Omar": 1216533
        }
    },
    "The Pharaohs": {
        "players": {
            "Hussein": 275539,
            "Ahmed": 1545049
        }
    },
    "Smokers": {
        "players": {
            "Body": 1679155,
            "Tawheed": 2208753
        }
    },
    "Falcons": {
        "players": {
            "Nasser": 2134302,
            "Mahmoud": 8656672
        }
    },
    "Boys": {
        "players": {
            "Ali": 8703485,
            "Magdy": 5857198
        }
    }
}

# جدول المباريات (نفس اللي عندك)
FIXTURES = {}
for cycle in range(7):
    base = cycle * 5
    FIXTURES.update({
        base+1: [("Black Tech", "The Pharaohs"), ("Smokers", "Falcons"), ("Boys", "BYE")],
        base+2: [("Smokers", "The Pharaohs"), ("Black Tech", "Boys"), ("Falcons", "BYE")],
        base+3: [("Smokers", "Black Tech"), ("Falcons", "Boys"), ("The Pharaohs", "BYE")],
        base+4: [("Smokers", "Boys"), ("Falcons", "The Pharaohs"), ("Black Tech", "BYE")],
        base+5: [("Falcons", "Black Tech"), ("The Pharaohs", "Boys"), ("Smokers", "BYE")]
    })
FIXTURES[11] = [("BYE", "BYE")]
FIXTURES[22] = [("BYE", "BYE")]
FIXTURES[38] = [("BYE", "BYE")]

# ==============================================
#  جلب الجولة الحالية
# ==============================================

def get_current_gw():
    url = "https://fantasy.premierleague.com/api/bootstrap-static/"
    try:
        data = requests.get(url).json()
        current = next((e for e in data["events"] if e["is_current"]), None)
        if current:
            return current["id"]
        # لو مفيش جولة حالية، نأخذ آخر جولة منتهية
        finished = [e["id"] for e in data["events"] if e["finished"]]
        return max(finished) if finished else 1
    except:
        return 1

# ==============================================
#  جلب نقاط لاعب معين في جولة معينة
# ==============================================

def get_player_points(player_id, gw):
    url = f"https://fantasy.premierleague.com/api/entry/{player_id}/event/{gw}/picks/"
    try:
        data = requests.get(url).json()
        entry = data.get("entry_history", {})
        points = entry.get("points", 0)
        transfer_cost = entry.get("event_transfers_cost", 0)
        return points - transfer_cost
    except:
        return 0

# ==============================================
#  الحساب الرئيسي
# ==============================================

def calculate_standings():
    current_gw = get_current_gw()
    print(f"الجولة الحالية/الأخيرة: {current_gw}")

    team_stats = {
        team: {
            "name": team,
            "played": 0,
            "won": 0,
            "draw": 0,
            "lost": 0,
            "matchPts": 0,
            "bonus": 0,
            "total": 0
        }
        for team in TEAMS
    }

    player_stats = {}
    for team, data in TEAMS.items():
        for name, pid in data["players"].items():
            player_stats[pid] = {
                "name": name,
                "team": team,
                "points": 0
            }

    # حساب كل جولة حتى الجولة الحالية
    for gw in range(1, current_gw + 1):
        team_points_this_gw = {}

        # جلب نقاط كل فريق في الجولة دي
        for team, data in TEAMS.items():
            total = 0
            for pid in data["players"].values():
                pts = get_player_points(pid, gw)
                total += pts
                # نقاط اللاعبين تتراكم دائمًا (حسب رغبتك السابقة)
                player_stats[pid]["points"] += pts
            team_points_this_gw[team] = total

        # البونص: أعلى فريق في الجولة (حتى لو في راحة)
        if team_points_this_gw:
            max_score = max(team_points_this_gw.values())
            top_teams = [t for t, s in team_points_this_gw.items() if s == max_score]
            if len(top_teams) == 1:
                team_stats[top_teams[0]]["bonus"] += 1

        # نتائج المباريات
        if gw in FIXTURES:
            for match in FIXTURES[gw]:
                t1, t2 = match
                if t1 == "BYE" or t2 == "BYE":
                    continue

                s1 = team_points_this_gw.get(t1, 0)
                s2 = team_points_this_gw.get(t2, 0)

                team_stats[t1]["played"] += 1
                team_stats[t2]["played"] += 1

                if s1 > s2:
                    team_stats[t1]["won"] += 1
                    team_stats[t1]["matchPts"] += 3
                    team_stats[t2]["lost"] += 1
                elif s2 > s1:
                    team_stats[t2]["won"] += 1
                    team_stats[t2]["matchPts"] += 3
                    team_stats[t1]["lost"] += 1
                else:
                    team_stats[t1]["draw"] += 1
                    team_stats[t2]["draw"] += 1
                    team_stats[t1]["matchPts"] += 1
                    team_stats[t2]["matchPts"] += 1

    # تحديث الإجمالي لكل فريق
    for team in team_stats:
        team_stats[team]["total"] = team_stats[team]["matchPts"] + team_stats[team]["bonus"]

    # ترتيب الفرق
    sorted_teams = sorted(
        team_stats.values(),
        key=lambda x: (-x["total"], -x["matchPts"])
    )

    # ترتيب اللاعبين
    sorted_players = sorted(
        player_stats.values(),
        key=lambda x: -x["points"]
    )

    # النتيجة النهائية
    output = {
        "current_gw": current_gw,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "teams": sorted_teams,
        "players": sorted_players
    }

    # حفظ الملف
    os.makedirs("data", exist_ok=True)
    with open("data/current_standings.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"تم تحديث data/current_standings.json - الجولة {current_gw}")

if __name__ == "__main__":
    calculate_standings()
