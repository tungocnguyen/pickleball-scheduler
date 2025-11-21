# pickleball_local_app_minrepeat.py
import os
os.environ["STREAMLIT_PANDAS_SHIM"] = "v1"
os.environ["STREAMLIT_DISABLE_PYARROW"] = "1"


import streamlit as st
import pandas as pd
import os
import json
import io
from itertools import combinations
from openpyxl import Workbook
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

# -----------------------
# Filenames & constants
# -----------------------
DATA_DIR = "."
MEMBERS_CSV = os.path.join(DATA_DIR, "members.csv")
SETTINGS_JSON = os.path.join(DATA_DIR, "settings.json")
PARTNER_HISTORY_JSON = os.path.join(DATA_DIR, "partner_history.json")

COURT_SIZE = 4
LEVELS = ["Beginner", "Intermediate", "Advanced"]
LEVEL_MAP = {"Beginner": 1, "Intermediate": 2, "Advanced": 3}

# -----------------------
# Helpers: ensure default files
# -----------------------
def ensure_files_exist():
    if not os.path.exists(MEMBERS_CSV):
        sample = pd.DataFrame([
            {"name":"Alice","present":False,"level":"Intermediate"},
            {"name":"Bob","present":False,"level":"Beginner"},
            {"name":"Carol","present":False,"level":"Advanced"},
            {"name":"David","present":False,"level":"Intermediate"},
            {"name":"Emma","present":False,"level":"Beginner"},
            {"name":"Frank","present":False,"level":"Intermediate"},
            {"name":"George","present":False,"level":"Advanced"},
            {"name":"Helen","present":False,"level":"Intermediate"},
            {"name":"Ian","present":False,"level":"Beginner"},
            {"name":"Jane","present":False,"level":"Intermediate"},
        ])
        sample.to_csv(MEMBERS_CSV, index=False)

    if not os.path.exists(SETTINGS_JSON):
        default = {
            "total_minutes": 150,
            "game_minutes": 15,
            "break_count": 1,
            "break_minutes": 5,
            "enable_king_courts": False,
            "king_court_winners": 2,
            "enable_min_repeat": True,
            "max_pair_repeat_target": 1  # target maximum repeats per pair (soft target)
        }
        with open(SETTINGS_JSON, "w") as f:
            json.dump(default, f, indent=2)

    if not os.path.exists(PARTNER_HISTORY_JSON):
        with open(PARTNER_HISTORY_JSON, "w") as f:
            json.dump({}, f, indent=2)

def load_members():
    ensure_files_exist()
    df = pd.read_csv(MEMBERS_CSV)
    if "present" not in df.columns:
        df["present"] = False
    if "level" not in df.columns:
        df["level"] = LEVELS[0]
    df["present"] = df["present"].astype(bool)
    return df

def save_members(df):
    df.to_csv(MEMBERS_CSV, index=False)

def load_settings():
    ensure_files_exist()
    with open(SETTINGS_JSON, "r") as f:
        return json.load(f)

def save_settings(settings):
    with open(SETTINGS_JSON, "w") as f:
        json.dump(settings, f, indent=2)

def load_partner_history():
    ensure_files_exist()
    with open(PARTNER_HISTORY_JSON, "r") as f:
        return json.load(f)

def save_partner_history(hist):
    with open(PARTNER_HISTORY_JSON, "w") as f:
        json.dump(hist, f, indent=2)

# -----------------------
# Partner-history utilities
# -----------------------
def pair_key(a, b):
    # canonical key for pair
    return "|".join(sorted([a, b]))

def increment_partner_counts(partner_history, teams):
    # teams: list of lists representing teams of 2 (e.g. [['A','B'], ['C','D']])
    for team in teams:
        if len(team) < 2:
            continue
        k = pair_key(team[0], team[1])
        partner_history[k] = partner_history.get(k, 0) + 1

# -----------------------
# Scheduler: weighted + min-repeat
# -----------------------
def best_pairing_for_four(names_with_levels, partner_history, enable_min_repeat):
    """
    names_with_levels: [(name, level_str)...] length 4
    partner_history: dict(pair_key->count)
    enable_min_repeat: if True, pairing cost includes partner_history counts
    Returns teams: [[p1,p2],[p3,p4]]
    """
    nv = [(n, LEVEL_MAP.get(l, 1)) for n, l in names_with_levels]
    pairings = [((0,1),(2,3)), ((0,2),(1,3)), ((0,3),(1,2))]
    best = None
    best_score = None
    for p1,p2 in pairings:
        team1_names = (nv[p1[0]][0], nv[p1[1]][0])
        team2_names = (nv[p2[0]][0], nv[p2[1]][0])
        team1_level = nv[p1[0]][1] + nv[p1[1]][1]
        team2_level = nv[p2[0]][1] + nv[p2[1]][1]
        level_diff = abs(team1_level - team2_level)
        # partner repeat cost
        repeat_cost = 0
        if enable_min_repeat:
            repeat_cost += partner_history.get(pair_key(team1_names[0], team1_names[1]), 0)
            repeat_cost += partner_history.get(pair_key(team2_names[0], team2_names[1]), 0)
        # compound score: prefer low repeat_cost first, then low level_diff
        score = (repeat_cost, level_diff)
        if best_score is None or score < best_score:
            best_score = score
            best = ([list(team1_names), list(team2_names)], score)
    return best[0]  # teams

def create_rotation_schedule_minrepeat(members_present, num_courts, total_minutes, game_minutes,
                                       break_count, break_minutes, partner_history, enable_min_repeat):
    total_break_time = break_count * break_minutes
    game_time_available = total_minutes - total_break_time
    num_rounds = max(0, game_time_available // game_minutes)
    players = [m["name"] for m in members_present]
    name_to_level = {m["name"]: m["level"] for m in members_present}
    games_played = {p:0 for p in players}
    schedule = []

    break_positions = []
    if break_count > 0 and num_rounds > 0:
        interval = max(1, num_rounds // (break_count + 1))
        break_positions = [(i+1)*interval for i in range(break_count)]

    # For each round:
    for r in range(1, num_rounds + 1):
        if r in break_positions:
            schedule.append("BREAK")
            continue

        spots = num_courts * COURT_SIZE
        available_players = players.copy()
        chosen = []
        # pick players fairly by games_played
        for _ in range(min(spots, len(available_players))):
            sorted_candidates = sorted([p for p in available_players if p not in chosen],
                                       key=lambda p: (games_played.get(p,0), p))
            if not sorted_candidates: break
            pick = sorted_candidates[0]
            chosen.append(pick)
            games_played[pick] += 1

        # Now group chosen players into courts of 4 using greedy min-repeat grouping:
        courts = []
        remaining = chosen.copy()

        while remaining:
            if len(remaining) < COURT_SIZE:
                # leftover small court
                courts.append(remaining.copy())
                break

            # consider all 4-combinations from remaining and pick one with minimal partner-history cost (using best pairing inside)
            best_comb = None
            best_comb_score = None
            for comb in combinations(remaining, COURT_SIZE):
                # compute minimal pairing score for this comb
                names_levels = [(name, name_to_level.get(name, "Beginner")) for name in comb]
                #teams, score = best_pairing_for_four(names_levels, partner_history, enable_min_repeat)
                teams = best_pairing_for_four(names_levels, partner_history, enable_min_repeat)           
            
            
            flat_group = teams[0] + teams[1]
            courts.append(flat_group)
            # remove those chosen from remaining
            for n in comb:
                remaining.remove(n)

        # Append round courts and update partner history counts for teammates
        schedule.append(courts)
        # Update partner history for this round's teammates
        for court in courts:
            if len(court) >= 4:
                # teams are positions [0,1] and [2,3]
                t1 = [court[0], court[1]]
                t2 = [court[2], court[3]]
                increment_partner_counts(partner_history, [t1, t2])
            else:
                # smaller courts: increment pairs for adjacent pairs (best-effort)
                for i in range(0, len(court)-1, 2):
                    increment_partner_counts(partner_history, [[court[i], court[i+1]]])

    return schedule, games_played, num_rounds, break_positions, partner_history

# -----------------------
# Export helpers
# -----------------------
def export_excel_bytes(schedule, games_played):
    wb = Workbook()
    ws = wb.active
    ws.title = "Pickleball Schedule"
    ws.append(["Round/Break", "Court", "Players"])
    round_num = 1
    for obj in schedule:
        if obj == "BREAK":
            ws.append(["BREAK", "", "Water Break"])
        else:
            for c, players in enumerate(obj, 1):
                if len(players) == 4:
                    team1 = ", ".join(players[0:2])
                    team2 = ", ".join(players[2:4])
                    players_text = f"{team1} vs {team2}"
                else:
                    players_text = ", ".join(players)
                ws.append([f"Round {round_num}", f"Court {c}", players_text])
            round_num += 1
    ws.append([])
    ws.append(["Player", "Games"])
    for p, g in games_played.items():
        ws.append([p, g])
    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream.getvalue()

def export_pdf_bytes(schedule, games_played):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    elements = []
    styles = getSampleStyleSheet()
    elements.append(Paragraph("<b>Pickleball Schedule</b>", styles["Title"]))
    elements.append(Spacer(1, 12))
    table_data = [["Round/Break", "Court", "Players"]]
    round_num = 1
    for obj in schedule:
        if obj == "BREAK":
            table_data.append(["BREAK", "", "Water Break"])
        else:
            for c, players in enumerate(obj, 1):
                if len(players) == 4:
                    team1 = ", ".join(players[0:2])
                    team2 = ", ".join(players[2:4])
                    players_text = f"{team1} vs {team2}"
                else:
                    players_text = ", ".join(players)
                table_data.append([f"Round {round_num}", f"Court {c}", players_text])
            round_num += 1
    t = Table(table_data, repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                           ("GRID", (0, 0), (-1, -1), 1, colors.black)]))
    elements.append(t)
    elements.append(Spacer(1, 20))
    elements.append(Paragraph("<b>Games Per Player</b>", styles["Heading2"]))
    summary = [["Player", "Games"]] + [[p, g] for p, g in games_played.items()]
    t2 = Table(summary)
    t2.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                            ("GRID", (0, 0), (-1, -1), 1, colors.black)]))
    elements.append(t2)
    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()

# -----------------------
# Streamlit UI
# -----------------------
st.set_page_config(page_title="Pickleball Local Scheduler (Min-Repeat)", layout="wide")
st.title("🏓 Pickleball Local Scheduler — Minimum Repeat Partner Enabled")

ensure_files_exist()

# Load data
members_df = load_members()
settings = load_settings()
partner_history = load_partner_history()

# Sidebar: Settings & member management
st.sidebar.header("Settings")
total_minutes = st.sidebar.number_input("Total session time (minutes)", value=settings.get("total_minutes", 150), min_value=30, step=5)
game_minutes = st.sidebar.number_input("Minutes per game", value=settings.get("game_minutes", 15), min_value=5, step=1)
break_count = st.sidebar.number_input("Number of breaks", value=settings.get("break_count", 1), min_value=0, step=1)
break_minutes = st.sidebar.number_input("Minutes per break", value=settings.get("break_minutes", 5), min_value=1, step=1)
enable_king_courts = st.sidebar.checkbox("Enable King Courts (rotate winners)", value=settings.get("enable_king_courts", False))
king_court_winners = st.sidebar.number_input("Number of winners per court to rotate", value=settings.get("king_court_winners", 2), min_value=1, step=1)
enable_min_repeat = st.sidebar.checkbox("Enable minimum repeat partner avoidance", value=settings.get("enable_min_repeat", True))
max_pair_repeat_target = st.sidebar.number_input("Max pair repeats target (soft)", value=settings.get("max_pair_repeat_target", 1), min_value=0, step=1)

if st.sidebar.button("Save Settings"):
    new_s = {
        "total_minutes": int(total_minutes),
        "game_minutes": int(game_minutes),
        "break_count": int(break_count),
        "break_minutes": int(break_minutes),
        "enable_king_courts": bool(enable_king_courts),
        "king_court_winners": int(king_court_winners),
        "enable_min_repeat": bool(enable_min_repeat),
        "max_pair_repeat_target": int(max_pair_repeat_target)
    }
    save_settings(new_s)
    st.success("Settings saved.")

st.sidebar.header("Member Management")
st.sidebar.subheader("Add member")
with st.sidebar.form("add"):
    new_name = st.text_input("Name")
    new_level = st.selectbox("Level", LEVELS)
    add_sub = st.form_submit_button("Add")
    if add_sub and new_name.strip():
        if new_name.strip().lower() in members_df["name"].str.lower().tolist():
            st.warning("Member exists.")
        else:
            members_df = members_df.append({"name": new_name.strip(), "present": False, "level": new_level}, ignore_index=True)
            save_members(members_df)
            st.experimental_rerun()

st.sidebar.subheader("Remove member")
remove_choice = st.sidebar.selectbox("Remove", options=["(none)"] + members_df["name"].tolist())
if st.sidebar.button("Remove"):
    if remove_choice != "(none)":
        members_df = members_df[members_df["name"] != remove_choice]
        save_members(members_df)
        st.experimental_rerun()

if st.sidebar.button("Reset partner history"):
    partner_history = {}
    save_partner_history(partner_history)
    st.sidebar.success("Partner history cleared.")

# Main: Attendance
st.header("Attendance / Check-in")
members_df = load_members()
col1, col2 = st.columns([3,1])
present_updates = {}
for _, row in members_df.iterrows():
    name = row["name"]
    key = f"att__{name}"        # stable key using the name (avoid spaces/specials if you like)
    # if name contains special chars you could replace them, e.g. key = "att__" + name.replace(" ", "_")
    present_updates[name] = st.checkbox(f'{name} ({row["level"]})', value=bool(row["present"]), key=key)


if st.button("Save Attendance"):
    members_df = load_members()   # reload fresh copy
    for name, pres in present_updates.items():
        members_df.loc[members_df["name"] == name, "present"] = bool(pres)
    save_members(members_df)
    # clear any schedule in session_state so UI refreshes consistently
    for k in ("schedule", "games_played", "num_rounds", "break_positions"):
        if k in st.session_state:
            del st.session_state[k]
    st.success("Attendance saved.")
    st.experimental_rerun()


# Scheduler controls
st.header("Scheduler")
members_df = load_members()
players_present = members_df[members_df["present"]].to_dict("records")
st.write(f"Present players: **{len(players_present)}**")
num_courts = st.number_input("Number of courts", min_value=1, step=1, value=2)

if st.button("Generate"):
    if len(players_present) < 4:
        st.error("Need at least 4 players.")
    else:
        # use current settings
        schedule, games_played, num_rounds, break_positions, updated_history = create_rotation_schedule_minrepeat(
            players_present, num_courts, total_minutes, game_minutes, break_count, break_minutes,
            partner_history.copy(), enable_min_repeat
        )
        # update global partner history file with increments from this generation
        partner_history = updated_history
        save_partner_history(partner_history)

        if enable_king_courts:
            # rotate winners (based on positions) if desired
            def rotate_king(schedule, winners_per_court):
                for r in range(len(schedule)-1):
                    if schedule[r] == "BREAK" or schedule[r+1] == "BREAK":
                        continue
                    cur = schedule[r]
                    nxt = schedule[r+1]
                    for c in range(min(len(cur), len(nxt))):
                        if len(cur[c]) >= winners_per_court and len(nxt[c]) >= winners_per_court:
                            winners = cur[c][:winners_per_court]
                            nxt_idx = (c+1) % len(nxt)
                            nxt[nxt_idx][:winners_per_court] = winners
                return schedule
            schedule = rotate_king(schedule, king_court_winners)

        st.session_state["schedule"] = schedule
        st.session_state["games_played"] = games_played
        st.session_state["num_rounds"] = num_rounds
        st.success(f"Generated {num_rounds} rounds (breaks at: {break_positions})")

# Display grid
if "schedule" in st.session_state:
    schedule = st.session_state["schedule"]
    st.subheader("Schedule Grid")
    max_courts = max((len(r) for r in schedule if r != "BREAK"), default=0)
    cols = ["Round"] + [f"Court {i+1}" for i in range(max_courts)]
    rows = []
    round_idx = 1
    for obj in schedule:
        if obj == "BREAK":
            row = {"Round": "BREAK"}
            for c in range(max_courts):
                row[f"Court {c+1}"] = ""
            rows.append(row)
        else:
            row = {"Round": f"Round {round_idx}"}
            for c in range(max_courts):
                if c < len(obj):
                    players = obj[c]
                    if len(players) >= 4:
                        row[f"Court {c+1}"] = f"{players[0]} + {players[1]}  vs  {players[2]} + {players[3]}"
                    else:
                        row[f"Court {c+1}"] = ", ".join(players)
                else:
                    row[f"Court {c+1}"] = ""
            rows.append(row)
            round_idx += 1

    grid_df = pd.DataFrame(rows, columns=cols)
    # T.N. 2025-11-21 10:55:04.161 Please replace `use_container_width` with `width`.
    # For `use_container_width=True`, use `width='stretch'`. For `use_container_width=False`, use `width='content'`
    #st.dataframe(grid_df, use_container_width=True)
    st.dataframe(grid_df, width='stretch')

    # Summary at bottom
    st.subheader("Games per Player")
    games_played = st.session_state.get("games_played", {})
    for p in [m["name"] for m in players_present]:
        games_played.setdefault(p, 0)
    summary_df = pd.DataFrame([{"Player": p, "Games": games_played[p]} for p in sorted(games_played.keys())])
    st.table(summary_df)

    # Downloads
    excel_bytes = export_excel_bytes(schedule, games_played)
    pdf_bytes = export_pdf_bytes(schedule, games_played)
    st.download_button("📘 Download Excel", data=excel_bytes, file_name="schedule.xlsx")
    st.download_button("📄 Download PDF", data=pdf_bytes, file_name="schedule.pdf")
