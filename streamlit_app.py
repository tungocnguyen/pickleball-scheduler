import os
os.environ["STREAMLIT_PANDAS_SHIM"] = "v1"
os.environ["STREAMLIT_DISABLE_PYARROW"] = "1"

import streamlit as st
import pandas as pd
import json
import io
from itertools import combinations
from openpyxl import Workbook
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
import gspread
from google.oauth2.service_account import Credentials

# -------------------------
# GOOGLE SHEETS CONNECTION
# -------------------------

def get_gs_client():
    creds_info = st.secrets["gcp_service_account"]
    credentials = Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(credentials)

gc = get_gs_client()

MEMBERS_SHEET_ID = st.secrets["sheets"]["members_sheet"]
SETTINGS_SHEET_ID = st.secrets["sheets"]["settings_sheet"]
HISTORY_SHEET_ID = st.secrets["sheets"]["history_sheet"]

def load_members():
    ws = gc.open_by_key(MEMBERS_SHEET_ID).worksheet("members")
    df = pd.DataFrame(ws.get_all_records())
    if df.empty:
        df = pd.DataFrame(columns=["name", "present", "level"])
    df["present"] = df["present"].astype(bool)
    return df

def save_members(df):
    ws = gc.open_by_key(MEMBERS_SHEET_ID).worksheet("members")
    ws.clear()
    ws.update([df.columns.tolist()] + df.values.tolist())

def load_settings():
    ws = gc.open_by_key(SETTINGS_SHEET_ID).worksheet("settings")
    rows = ws.get_all_records()
    if not rows:
        return {}
    return {r["key"]: r["value"] for r in rows}

def save_settings(settings):
    ws = gc.open_by_key(SETTINGS_SHEET_ID).worksheet("settings")
    ws.clear()
    rows = [{"key": k, "value": v} for k, v in settings.items()]
    df = pd.DataFrame(rows)
    ws.update([df.columns.tolist()] + df.values.tolist())

def load_partner_history():
    ws = gc.open_by_key(HISTORY_SHEET_ID).worksheet("history")
    rows = ws.get_all_records()
    return {r["pair"]: int(r["count"]) for r in rows} if rows else {}

def save_partner_history(hist):
    ws = gc.open_by_key(HISTORY_SHEET_ID).worksheet("history")
    ws.clear()
    rows = [{"pair": k, "count": v} for k, v in hist.items()]
    df = pd.DataFrame(rows)
    ws.update([df.columns.tolist()] + df.values.tolist())

# -------------------------
# CONSTANTS
# -------------------------
COURT_SIZE = 4
LEVELS = ["Beginner", "Intermediate", "Advanced"]
LEVEL_MAP = {"Beginner": 1, "Intermediate": 2, "Advanced": 3}

# -------------------------
# PAIR FUNCTIONS
# -------------------------
def pair_key(a, b):
    return "|".join(sorted([a, b]))

def increment_partner_counts(partner_history, teams):
    for t in teams:
        if len(t) == 2:
            k = pair_key(t[0], t[1])
            partner_history[k] = partner_history.get(k, 0) + 1

# -------------------------
# BEST PAIRING FOR FOUR
# -------------------------
def best_pairing_for_four(names_with_levels, partner_history, avoid_repeat):
    nv = [(n, LEVEL_MAP.get(l, 1)) for n, l in names_with_levels]
    pairings = [((0,1),(2,3)), ((0,2),(1,3)), ((0,3),(1,2))]
    best = None
    best_score = None

    for p1,p2 in pairings:
        t1 = (nv[p1[0]][0], nv[p1[1]][0])
        t2 = (nv[p2[0]][0], nv[p2[1]][0])
        lvl_diff = abs(nv[p1[0]][1] + nv[p1[1]][1] - (nv[p2[0]][1] + nv[p2[1]][1]))
        repeat_cost = 0
        if avoid_repeat:
            repeat_cost += partner_history.get(pair_key(*t1), 0)
            repeat_cost += partner_history.get(pair_key(*t2), 0)
        score = (repeat_cost, lvl_diff)
        if best_score is None or score < best_score:
            best_score = score
            best = [list(t1), list(t2)]
    return best

# -------------------------
# SCHEDULER
# -------------------------
def create_rotation_schedule_minrepeat(members_present, num_courts, total_minutes, game_minutes,
                                       break_count, break_minutes, partner_history, avoid_repeat):

    total_break_time = break_count * break_minutes
    game_time_available = total_minutes - total_break_time
    num_rounds = max(0, game_time_available // game_minutes)

    players = [m["name"] for m in members_present]
    name_to_level = {m["name"]: m["level"] for m in members_present}
    games_played = {p: 0 for p in players}
    schedule = []

    break_positions = []
    if break_count > 0:
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
            if len(remaining) < 4:
                courts.append(remaining.copy())
                break

            best_comb = None
            best_score = None

            for comb in combinations(remaining, 4):
                pairs = best_pairing_for_four(
                    [(n, name_to_level[n]) for n in comb],
                    partner_history,
                    avoid_repeat,
                )
                t1, t2 = pairs
                repeat_cost = (
                    partner_history.get(pair_key(*t1), 0) +
                    partner_history.get(pair_key(*t2), 0)
                )
                level_diff = abs(
                    LEVEL_MAP[name_to_level[t1[0]]] + LEVEL_MAP[name_to_level[t1[1]]] -
                    (LEVEL_MAP[name_to_level[t2[0]]] + LEVEL_MAP[name_to_level[t2[1]]])
                )
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
                increment_partner_counts(partner_history,
                                         [[court[0], court[1]], [court[2], court[3]]])

    return schedule, games_played, num_rounds, break_positions, partner_history

# -------------------------
# EXPORTERS
# -------------------------
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
                if len(players)==4:
                    txt = f"{players[0]} + {players[1]} vs {players[2]} + {players[3]}"
                else:
                    txt = ", ".join(players)
                ws.append([f"Round {rn}", f"Court {c}", txt])
            rn+=1
    ws.append([])
    ws.append(["Player","Games"])
    for p,g in games_played.items():
        ws.append([p,g])
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
    els.append(Spacer(1,12))

    tbl = [["Round/Break","Court","Players"]]
    rn=1
    for obj in schedule:
        if obj=="BREAK":
            tbl.append(["BREAK","","Water Break"])
        else:
            for c,players in enumerate(obj,1):
                if len(players)==4:
                    txt = f"{players[0]} + {players[1]} vs {players[2]} + {players[3]}"
                else: txt=", ".join(players)
                tbl.append([f"Round {rn}", f"Court {c}", txt])
            rn+=1

    t = Table(tbl, repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0,0),(-1,0), colors.lightgrey),
                           ("GRID",(0,0),(-1,-1),1, colors.black)]))
    els.append(t)
    els.append(Spacer(1,24))

    els.append(Paragraph("Games Per Player", styles["Heading2"]))
    summ = [["Player","Games"]]+[[p,g] for p,g in games_played.items()]
    t2=Table(summ)
    t2.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.lightgrey),
                            ("GRID",(0,0),(-1,-1),1, colors.black)]))
    els.append(t2)

    doc.build(els)
    buf.seek(0)
    return buf.getvalue()

# -------------------------
# STREAMLIT UI
# -------------------------
st.set_page_config(page_title="Online Pickleball Scheduler", layout="wide")
st.title("🏓 Pickleball Scheduler — Online (Google Sheets)")

members_df = load_members()
settings = load_settings()
history = load_partner_history()

# SIDEBAR SETTINGS
st.sidebar.header("Settings")
total_minutes = st.sidebar.number_input("Total session (minutes)", value=int(settings.get("total_minutes",150)))
game_minutes = st.sidebar.number_input("Minutes per game", value=int(settings.get("game_minutes",15)))
break_count  = st.sidebar.number_input("Break count", value=int(settings.get("break_count",1)))
break_minutes= st.sidebar.number_input("Break minutes", value=int(settings.get("break_minutes",5)))
avoid_repeat = st.sidebar.checkbox("Avoid repeat partners", value=str(settings.get("enable_min_repeat","True"))=="True")

if st.sidebar.button("Save Settings"):
    new_s = {
        "total_minutes": total_minutes,
        "game_minutes": game_minutes,
        "break_count": break_count,
        "break_minutes": break_minutes,
        "enable_min_repeat": str(avoid_repeat)
    }
    save_settings(new_s)
    st.success("Settings saved")

# ATTENDANCE
st.header("Attendance")
members_df = load_members()
present_updates = {}

for _, row in members_df.iterrows():
    nm = row["name"]
    present_updates[nm] = st.checkbox(f"{nm} ({row['level']})", value=row["present"], key=f"att__{nm}")

if st.button("Save Attendance"):
    members_df["present"] = members_df["name"].apply(lambda n: present_updates[n])
    save_members(members_df)
    st.success("Saved.")
    st.experimental_rerun()

# Scheduler
st.header("Scheduler")
present = members_df[members_df["present"]].to_dict("records")
st.write(f"Players present: **{len(present)}**")

num_courts = st.number_input("Number of courts", 1, 20, 2)

if st.button("Generate Schedule"):
    if len(present) < 4:
        st.error("Need at least 4 players.")
    else:
        schedule, games_played, num_rounds, break_positions, updated_hist = \
            create_rotation_schedule_minrepeat(
                present, num_courts, total_minutes, game_minutes,
                break_count, break_minutes, history.copy(), avoid_repeat
            )

        save_partner_history(updated_hist)

        st.session_state["schedule"] = schedule
        st.session_state["games_played"] = games_played

# DISPLAY
if "schedule" in st.session_state:
    schedule = st.session_state["schedule"]
    max_c = max((len(r) for r in schedule if r!="BREAK"), default=0)
    cols = ["Round"]+[f"Court {i+1}" for i in range(max_c)]
    rows=[]
    rnd=1
    for obj in schedule:
        if obj=="BREAK":
            row={"Round":"BREAK", **{f"Court {i+1}":"" for i in range(max_c)}}
            rows.append(row)
        else:
            row={"Round":f"Round {rnd}"}
            for c in range(max_c):
                if c < len(obj):
                    p=obj[c]
                    if len(p)>=4:
                        row[f"Court {c+1}"]=f"{p[0]} + {p[1]} vs {p[2]} + {p[3]}"
                    else:
                        row[f"Court {c+1}"]=", ".join(p)
                else:
                    row[f"Court {c+1}"]=""
            rows.append(row)
            rnd+=1

    df=pd.DataFrame(rows,columns=cols)
    st.dataframe(df, width='stretch')

    st.subheader("Games per Player")
    g=st.session_state["games_played"]
    summary=pd.DataFrame([{"Player":p,"Games":g[p]} for p in sorted(g.keys())])
    st.table(summary)

    excel_bytes = export_excel_bytes(schedule, g)
    pdf_bytes   = export_pdf_bytes(schedule, g)

    st.download_button("📘 Download Excel", excel_bytes, "schedule.xlsx")
    st.download_button("📄 Download PDF", pdf_bytes, "schedule.pdf")
