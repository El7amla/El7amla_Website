import json
import os
from datetime import datetime

# =======================
#  الإعدادات
# =======================

DATA_FOLDER = "fpl_data"  # المجلد اللي فيه gw1.json, gw2.json, ...

FIXTURES = {
    1: [("Black Tech", "The Pharaohs"), ("Smokers", "Falcons"), ("Boys", "BYE")],
    2: [("Smokers", "The Pharaohs"), ("Black Tech", "Boys"), ("Falcons", "BYE")],
    3: [("Smokers", "Black Tech"), ("Falcons", "Boys"), ("The Pharaohs", "BYE")],
    4: [("Smokers", "Boys"), ("Falcons", "The Pharaohs"), ("Black Tech", "BYE")],
    5: [("Falcons", "Black Tech"), ("The Pharaohs", "Boys"), ("Smokers", "BYE")],
}

# تكرار الدورة 7 مرات + جولات الراحة
full_fixtures = {}
for cycle in range(7):
    base = cycle * 5
    for i in range(5):
        gw = base + i + 1
        if gw <= 35:
            full_fixtures[gw] = FIXTURES[i + 1]

full_fixtures[11] = [("BYE", "BYE")]
full_fixtures[22] = [("BYE", "BYE")]
full_fixtures[38] = [("BYE", "BYE")]

# =======================
#  الدوال المساعدة
# =======================

def get_gw_files():
    """يرجع قائمة بمسارات ملفات gwX.json"""
    files = []
    for f in os.listdir(DATA_FOLDER):
        if f.startswith("gw") and f.endswith(".json"):
            try:
                gw_num = int(f[2:-5])
                files.append((gw_num, os.path.join(DATA_FOLDER, f)))
            except:
                pass
    files.sort(key=lambda x: x[0])
    return files

def load_gw_data(gw_file_path):
    """قراءة ملف جولة وتحويل النقاط"""
    if not os.path.exists(gw_file_path):
        return {}
    
    with open(gw_file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    team_points = {}
    for team, players in data.items():
        total = 0
        for pid, info in players.items():
            if isinstance(info, dict) and "points" in info:
                total += info["points"]
        team_points[team] = total
    
    return team_points

# =======================
#  الحساب الرئيسي
# =======================

def calculate_standings():
    team_stats = {
        t: {
            "name": t,
            "played": 0,
            "won": 0,
            "draw": 0,
            "lost": 0,
            "matchPts": 0,
            "bonus": 0,
            "total": 0
        }
        for t in ["Black Tech", "The Pharaohs", "Smokers", "Falcons", "Boys"]
    }

    player_stats = {}

    # قراءة جميع ملفات الجولات
    gw_files = get_gw_files()
    max_gw = max([g[0] for g in gw_files], default=0)

    for gw_num, gw_path in gw_files:
        team_points = load_gw_data(gw_path)
        if not team_points:
            continue

        # البونص: أعلى فريق في الجولة (حتى لو كان في راحة)
        if team_points:
            max_score = max(team_points.values())
            top_teams = [t for t, s in team_points.items() if s == max_score]
            if len(top_teams) == 1:
                team_stats[top_teams[0]]["bonus"] += 1

        # نتائج المباريات
        if gw_num in full_fixtures:
            for match in full_fixtures[gw_num]:
                t1, t2 = match
                if t1 == "BYE" or t2 == "BYE":
                    continue
                
                s1 = team_points.get(t1, 0)
                s2 = team_points.get(t2, 0)

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

    # تحديث الإجمالي
    for t in team_stats:
        team_stats[t]["total"] = team_stats[t]["matchPts"] + team_stats[t]["bonus"]

    # ترتيب الفرق
    sorted_teams = sorted(
        team_stats.values(),
        key=lambda x: (-x["total"], -x["matchPts"])
    )

    # نقاط اللاعبين (من ملفات gwX.json)
    sorted_players = []
    player_map = {}

    for gw_num, gw_path in gw_files:
        with open(gw_path, "r", encoding="utf-8") as f:
            gw_data = json.load(f)
        
        for team, players in gw_data.items():
            for pid, info in players.items():
                if not isinstance(info, dict) or "points" not in info:
                    continue
                points = info["points"]
                if pid not in player_map:
                    player_map[pid] = {
                        "name": f"Player {pid}",  # ← غيّر ده لو عندك أسماء حقيقية
                        "team": team,
                        "points": 0
                    }
                player_map[pid]["points"] += points

    sorted_players = sorted(
        player_map.values(),
        key=lambda x: -x["points"]
    )

    # النتيجة النهائية
    output = {
        "current_gw": max_gw,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "teams": sorted_teams,
        "players": sorted_players
    }

    # حفظ الملف
    os.makedirs("data", exist_ok=True)
    with open("data/current_standings.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"تم تحديث data/current_standings.json - الجولة {max_gw}")

if __name__ == "__main__":
    calculate_standings()
