# app.py
import os
# disable pyarrow usage in Streamlit (recommended for Windows & cloud stability)
os.environ["STREAMLIT_PANDAS_SHIM"] = "v1"
os.environ["STREAMLIT_DISABLE_PYARROW"] = "1"

import streamlit as st
import pandas as pd
import io
from itertools import combinations
from openpyxl import Workbook
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
import gspread
from google.oauth2.service_account import Credentials

# -------------
# Config (edit if desired)
# -------------
# Provided Google Sheet ID (from user)
#SHEET_ID = "1vYcy7ia-9F3MXcw4irI10M3uEm-Hs1GnsLc-RQdSPW8"
SHEET_ID = ""

# Tab names inside the Google Sheet
TAB_MEMBERS = "members"
TAB_SETTINGS = "settings"
TAB_HISTORY = "history"

# Constants
COURT_SIZE = 4
LEVELS = ["Beginner", "Intermediate", "Advanced"]
LEVEL_MAP = {"Beginner": 1, "Intermediate": 2, "Advanced": 3}

# -------------
# Google Sheets helpers
# -------------
def get_gs_client():
    try:
        creds_info = st.secrets["gcp_service_account"]
    except Exception as e:
        st.error("Missing gcp_service_account in Streamlit secrets. See README in app for setup steps.")
        st.stop()
    credentials = Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    return gspread.authorize(credentials)


def open_sheet_and_ensure_tabs(gc):
    """
    Opens the spreadsheet by ID. If expected worksheets (members, settings, history)
    do not exist, create them and add header rows.
    """
    try:
        SHEET_ID = st.secrets["sheets"]["google_sheet"]
        sh = gc.open_by_key(SHEET_ID)
    except Exception as e:
        st.error("Failed to open Google Sheet. Check the Sheet ID and that the service account has Editor access.")
        st.stop()

    # members
    try:
        ws_members = sh.worksheet(TAB_MEMBERS)
    except gspread.WorksheetNotFound:
        ws_members = sh.add_worksheet(title=TAB_MEMBERS, rows="1000", cols="10")
        ws_members.update("A1:C1", [["name", "present", "level"]])

    # settings
    try:
        ws_settings = sh.worksheet(TAB_SETTINGS)
    except gspread.WorksheetNotFound:
        ws_settings = sh.add_worksheet(title=TAB_SETTINGS, rows="50", cols="2")
        default_settings = [
            ["key", "value"],
            ["total_minutes", "150"],
            ["game_minutes", "15"],
            ["break_count", "1"],
            ["break_minutes", "5"],
            ["enable_min_repeat", "True"]
        ]
        ws_settings.update("A1:B6", default_settings)

    # history
    try:
        ws_history = sh.worksheet(TAB_HISTORY)
    except gspread.WorksheetNotFound:
        ws_history = sh.add_worksheet(title=TAB_HISTORY, rows="1000", cols="2")
        ws_history.update("A1:B1", [["pair", "count"]])

    return sh, ws_members, ws_settings, ws_history


def load_members(gc):
    _, ws_members, _, _ = open_sheet_and_ensure_tabs(gc)
    rows = ws_members.get_all_records()
    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(columns=["name", "present", "level"])
    if "present" not in df.columns:
        df["present"] = False
    if "level" not in df.columns:
        df["level"] = LEVELS[0]
    df["present"] = df["present"].astype(bool)
    return df


def save_members(gc, df):
    _, ws_members, _, _ = open_sheet_and_ensure_tabs(gc)
    ws_members.clear()
    ws_members.update([df.columns.tolist()] + df.fillna("").values.tolist())


def load_settings(gc):
    _, _, ws_settings, _ = open_sheet_and_ensure_tabs(gc)
    rows = ws_settings.get_all_records()
    if not rows:
        return {}
    return {r["key"]: r["value"] for r in rows}


def save_settings(gc, settings_dict):
    _, _, ws_settings, _ = open_sheet_and_ensure_tabs(gc)
    rows = [{"key": k, "value": v} for k, v in settings_dict.items()]
    df = pd.DataFrame(rows)
    ws_settings.clear()
    ws_settings.update([df.columns.tolist()] + df.fillna("").values.tolist())


def load_partner_history(gc):
    _, _, _, ws_history = open_sheet_and_ensure_tabs(gc)
    rows = ws_history.get_all_records()
    if not rows:
        return {}
    return {r["pair"]: int(r["count"]) for r in rows}


def save_partner_history(gc, hist):
    _, _, _, ws_history = open_sheet_and_ensure_tabs(gc)
    rows = [{"pair": k, "count": v} for k, v in hist.items()]
    df = pd.DataFrame(rows)
    ws_history.clear()
    ws_history.update([df.columns.tolist()] + df.values.tolist())


# -------------
# Pairing utilities and scheduler (same algorithm as local)
# -------------
def pair_key(a, b):
    return "|".join(sorted([a, b]))


def increment_partner_counts(partner_history, teams):
    for t in teams:
        if len(t) == 2:
            k = pair_key(t[0], t[1])
            partner_history[k] = partner_history.get(k, 0) + 1


def best_pairing_for_four(names_with_levels, partner_history, enable_min_repeat):
    nv = [(n, LEVEL_MAP.get(l, 1)) for n, l in names_with_levels]
    pairings = [((0,1),(2,3)), ((0,2),(1,3)), ((0,3),(1,2))]
    best = None
    best_score = None
    for p1,p2 in pairings:
        t1 = (nv[p1[0]][0], nv[p1[1]][0])
        t2 = (nv[p2[0]][0], nv[p2[1]][0])
        lvl_diff = abs(nv[p1[0]][1] + nv[p1[1]][1] - (nv[p2[0]][1] + nv[p2[1]][1]))
        repeat_cost = 0
        if enable_min_repeat:
            repeat_cost += partner_history.get(pair_key(*t1), 0)
            repeat_cost += partner_history.get(pair_key(*t2), 0)
        score = (repeat_cost, lvl_diff)
        if best_score is None or score < best_score:
            best_score = score
            best = [list(t1), list(t2)]
    return best


def create_rotation_schedule_minrepeat(members_present, num_courts, total_minutes, game_minutes,
                                       break_count, break_minutes, partner_history, enable_min_repeat):

    total_break_time = break_count * break_minutes
    game_time_available = total_minutes - total_break_time
    num_rounds = max(0, game_time_available // game_minutes)

    players = [m["name"] for m in members_present]
    name_to_level = {m["name"]: m["level"] for m in members_present}
    games_played = {p: 0 for p in players}
    schedule = []

    break_positions = []
    if break_count > 0 and num_rounds > 0:
        interval = max(1, num_rounds // (break_count + 1))
        break_positions = [(i+1)*interval for i in range(break_count)]

    for r in range(1, num_rounds+1):
        if r in break_positions:
            schedule.append("BREAK")
            continue

        spots = num_courts * COURT_SIZE
        chosen = []
        available = players.copy()

        for _ in range(min(spots, len(available))):
            cands = sorted([p for p in available if p not in chosen],
                           key=lambda p: (games_played[p], p))
            if not cands:
                break
            pick = cands[0]
            chosen.append(pick)
            games_played[pick] += 1

        courts = []
        remaining = chosen.copy()

        while remaining:
            if len(remaining) < COURT_SIZE:
                courts.append(remaining.copy())
                break

            best_comb = None
            best_score = None

            for comb in combinations(remaining, COURT_SIZE):
                pairs = best_pairing_for_four([(n, name_to_level[n]) for n in comb], partner_history, enable_min_repeat)
                t1, t2 = pairs
                repeat_cost = (partner_history.get(pair_key(*t1), 0) + partner_history.get(pair_key(*t2), 0))
                level_diff = abs(LEVEL_MAP[name_to_level[t1[0]]] + LEVEL_MAP[name_to_level[t1[1]]] -
                                 (LEVEL_MAP[name_to_level[t2[0]]] + LEVEL_MAP[name_to_level[t2[1]]]))
                score = (repeat_cost, level_diff)
                if best_score is None or score < best_score:
                    best_score = score
                    best_comb = (comb, t1, t2)

            comb, t1, t2 = best_comb
            flat = t1 + t2
            courts.append(flat)
            for p in comb:
                remaining.remove(p)

        schedule.append(courts)
        for court in courts:
            if len(court) >= 4:
                increment_partner_counts(partner_history, [[court[0], court[1]], [court[2], court[3]]])
            else:
                for i in range(0, len(court)-1, 2):
                    increment_partner_counts(partner_history, [[court[i], court[i+1]]])

    return schedule, games_played, num_rounds, break_positions, partner_history


# -------------
# EXPORT helpers (XLSX/PDF)
# -------------
def export_excel_bytes(schedule, games_played):
    wb = Workbook()
    ws = wb.active
    ws.append(["Round/Break", "Court", "Players"])
    rn = 1
    for obj in schedule:
        if obj == "BREAK":
            ws.append(["BREAK", "", "Water Break"])
        else:
            for c, players in enumerate(obj, 1):
                if len(players) == 4:
                    txt = f"{players[0]} + {players[1]} vs {players[2]} + {players[3]}"
                else:
                    txt = ", ".join(players)
                ws.append([f"Round {rn}", f"Court {c}", txt])
            rn += 1
    ws.append([])
    ws.append(["Player", "Games"])
    for p, g in games_played.items():
        ws.append([p, g])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


def export_pdf_bytes(schedule, games_played):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter)
    els = []
    styles = getSampleStyleSheet()
    els.append(Paragraph("Pickleball Schedule", styles["Title"]))
    els.append(Spacer(1, 12))

    tbl = [["Round/Break", "Court", "Players"]]
    rn = 1
    for obj in schedule:
        if obj == "BREAK":
            tbl.append(["BREAK", "", "Water Break"])
        else:
            for c, players in enumerate(obj, 1):
                if len(players) == 4:
                    txt = f"{players[0]} + {players[1]} vs {players[2]} + {players[3]}"
                else:
                    txt = ", ".join(players)
                tbl.append([f"Round {rn}", f"Court {c}", txt])
            rn += 1

    t = Table(tbl, repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                           ("GRID", (0, 0), (-1, -1), 1, colors.black)]))
    els.append(t)
    els.append(Spacer(1, 24))

    els.append(Paragraph("Games Per Player", styles["Heading2"]))
    summ = [["Player", "Games"]] + [[p, g] for p, g in games_played.items()]
    t2 = Table(summ)
    t2.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                            ("GRID", (0, 0), (-1, -1), 1, colors.black)]))
    els.append(t2)

    doc.build(els)
    buf.seek(0)
    return buf.getvalue()


# -------------
# Streamlit UI
# -------------
st.set_page_config(page_title="Pickleball Scheduler (Online)", layout="wide")
st.title("🏓 Pickleball Scheduler — Online (Google Sheets)")

# Create Google client and ensure tabs exist
gc = get_gs_client()
open_sheet_and_ensure_tabs(gc)

# load data
members_df = load_members(gc)
settings = load_settings(gc)
partner_history = load_partner_history(gc)

# Sidebar: settings
st.sidebar.header("Settings")
total_minutes = st.sidebar.number_input("Total session (minutes)", value=int(settings.get("total_minutes", 150)), min_value=10)
game_minutes = st.sidebar.number_input("Minutes per game", value=int(settings.get("game_minutes", 15)), min_value=1)
break_count = st.sidebar.number_input("Number of breaks", value=int(settings.get("break_count", 1)), min_value=0)
break_minutes = st.sidebar.number_input("Minutes per break", value=int(settings.get("break_minutes", 5)), min_value=1)
avoid_repeat = st.sidebar.checkbox("Avoid repeat partners", value=str(settings.get("enable_min_repeat", "True")) == "True")

if st.sidebar.button("Save Settings"):
    save_settings(gc, {
        "total_minutes": total_minutes,
        "game_minutes": game_minutes,
        "break_count": break_count,
        "break_minutes": break_minutes,
        "enable_min_repeat": str(avoid_repeat)
    })
    st.success("Settings saved to Google Sheet")

# Member management
st.sidebar.header("Members")
with st.sidebar.expander("Add member"):
    new_name = st.text_input("Name", key="new_name")
    new_level = st.selectbox("Level", LEVELS, key="new_level")
    if st.button("Add member"):
        df = load_members(gc)
        if new_name.strip() == "":
            st.warning("Enter a name.")
        elif new_name.strip().lower() in df["name"].str.lower().tolist():
            st.warning("Member already exists.")
        else:
            df = df.append({"name": new_name.strip(), "present": False, "level": new_level}, ignore_index=True)
            save_members(gc, df)
            st.success(f"Added {new_name.strip()}")
            st.experimental_rerun()

with st.sidebar.expander("Remove member"):
    df = load_members(gc)
    choice = st.selectbox("Select member to remove", options=["(none)"] + df["name"].tolist())
    if st.button("Remove selected"):
        if choice != "(none)":
            df = df[df["name"] != choice]
            save_members(gc, df)
            st.success(f"Removed {choice}")
            st.experimental_rerun()

if st.sidebar.button("Reset partner history"):
    partner_history = {}
    save_partner_history(gc, partner_history)
    st.experimental_rerun()

# Attendance on main page
st.header("Attendance / Check-in")
members_df = load_members(gc)
present_updates = {}
for _, row in members_df.iterrows():
    nm = row["name"]
    present_updates[nm] = st.checkbox(f"{nm} ({row['level']})", value=row["present"], key=f"att__{nm}")

if st.button("Save Attendance"):
    df = load_members(gc)
    df["present"] = df["name"].apply(lambda n: present_updates.get(n, False))
    save_members(gc, df)
    st.success("Attendance saved.")
    st.experimental_rerun()

# Scheduler controls
st.header("Scheduler")
present = load_members(gc)
present_players = present[present["present"]].to_dict("records")
st.write(f"Players present: **{len(present_players)}**")
num_courts = st.number_input("Number of courts", min_value=1, step=1, value=2)

if st.button("Generate Schedule"):
    if len(present_players) < 4:
        st.error("Need at least 4 players present.")
    else:
        schedule, games_played, num_rounds, break_positions, updated_history = create_rotation_schedule_minrepeat(
            present_players, num_courts, total_minutes, game_minutes, break_count, break_minutes, partner_history.copy(), avoid_repeat
        )
        save_partner_history(gc, updated_history)
        st.session_state["schedule"] = schedule
        st.session_state["games_played"] = games_played
        st.success(f"Generated {num_rounds} rounds (breaks at: {break_positions})")

# Display schedule
if "schedule" in st.session_state:
    schedule = st.session_state["schedule"]
    max_courts = max((len(r) for r in schedule if r != "BREAK"), default=0)
    cols = ["Round"] + [f"Court {i+1}" for i in range(max_courts)]
    rows = []
    rn = 1
    for obj in schedule:
        if obj == "BREAK":
            row = {"Round": "BREAK", **{f"Court {i+1}": "" for i in range(max_courts)}}
            rows.append(row)
        else:
            row = {"Round": f"Round {rn}"}
            for c in range(max_courts):
                if c < len(obj):
                    p = obj[c]
                    if len(p) >= 4:
                        row[f"Court {c+1}"] = f"{p[0]} + {p[1]} vs {p[2]} + {p[3]}"
                    else:
                        row[f"Court {c+1}"] = ", ".join(p)
                else:
                    row[f"Court {c+1}"] = ""
            rows.append(row)
            rn += 1

    df = pd.DataFrame(rows, columns=cols)
    st.dataframe(df, width='stretch')

    st.subheader("Games per Player")
    g = st.session_state.get("games_played", {})
    summary = pd.DataFrame([{"Player": p, "Games": g.get(p, 0)} for p in sorted(g.keys())])
    st.table(summary)

    excel_bytes = export_excel_bytes(schedule, g)
    pdf_bytes = export_pdf_bytes(schedule, g)

    st.download_button("📘 Download Excel", excel_bytes, file_name="schedule.xlsx")
    st.download_button("📄 Download PDF", pdf_bytes, file_name="schedule.pdf")

