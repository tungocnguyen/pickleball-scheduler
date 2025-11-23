"""
Streamlit Pickleball Scheduler (full)
Features:
- Load members/settings/schedule from a single Google Spreadsheet
- Auto-attempt Google Sheets load at startup; if it fails, fall back to a local cache file local_cache.json
- Show actual mode (online/offline) via the "Use Google Sheet" checkbox and status in the sidebar
- Member management with editable table using st.data_editor
- Settings tab to save last-used settings into the settings sheet/local cache
- Schedule generator with fairness heuristics (minimize partner/opponent repeats, balance game counts)
- Schedule display: rows = rounds, columns = courts with subcolumns Team 1 / Team 2 (e.g. "Alice / Cathy")
- Filters (level, gender, active, mixing, repeat partner)
- Export generated schedule to Excel

Secrets expectations (.streamlit/secrets.toml):
[gcp_service_account]
... (service account fields copied from Google JSON)

[google_sheets]
spreadsheet_id = "YOUR_SPREADSHEET_ID"
members_sheet = "members"
schedule_sheet = "schedule"
settings_sheet = "settings"
status_sheet = "status"

Run: streamlit run this_script.py
Author: Tu Nguyen
Date: November 23, 2025
Version: 0.5
pip install streamlit gspread google-auth openpyxl reportlab google-api-python-client google-auth-oauthlib google-auth-httplib2
run local streamlit run this_script_filename
"""

import streamlit as st
import pandas as pd
import numpy as np
import random
import math
import json
from io import BytesIO
from collections import Counter, defaultdict
import os
from io import BytesIO
import random

# Google API imports
from google.oauth2 import service_account
from googleapiclient.discovery import build

mixing_options = ["Random", "Level-based", "Minimize repeats"]
repeat_options = ["Avoid repeats", "Minimize", "No rule"]


# ---------------------- Google Sheets helpers ----------------------

def _build_service():
    """Build Google Sheets API service from st.secrets. Returns service or raises."""
    info = st.secrets.get("gcp_service_account")
    if not info:
        raise RuntimeError("gcp_service_account not found in st.secrets")
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"],
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return service


def load_google_sheet(sheet_name):
    """Load a whole worksheet by name. Return DataFrame or raise Exception."""
    service = _build_service()
    spreadsheet_id = st.secrets["google_sheets"]["spreadsheet_id"]
    # Read values; use the sheet name as range to get entire sheet
    resp = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=sheet_name).execute()
    values = resp.get("values", [])
    if not values:
        # return empty dataframe with no columns
        return pd.DataFrame()
    header, rows = values[0], values[1:]
    # Pad rows shorter than header
    norm_rows = [r + [""]*(len(header)-len(r)) if len(r) < len(header) else r[:len(header)] for r in rows]
    df = pd.DataFrame(norm_rows, columns=header)
    return df


def save_google_sheet(sheet_name, df):
    """Overwrite the worksheet with DataFrame df (including headers)."""
    service = _build_service()
    spreadsheet_id = st.secrets["google_sheets"]["spreadsheet_id"]
    upload_to_google_sheet(service, spreadsheet_id, sheet_name, df)

    # values = [df.columns.tolist()] + df.fillna("").astype(str).values.tolist()
    # body = {"values": values}
    # # Using update to replace range
    # service.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=sheet_name).execute()
    # service.spreadsheets().values().update(
    #     spreadsheetId=spreadsheet_id,
    #     range=sheet_name,
    #     valueInputOption="RAW",
    #     body=body,
    # ).execute()

def upload_to_google_sheet(service, spreadsheet_id, sheet_name, df):
    if df.empty:
        return

    # REMOVE INDEX
    df2 = df.copy().reset_index(drop=True)

    # FLATTEN MULTIINDEX
    if isinstance(df2.columns, pd.MultiIndex):
        df2.columns = [" ".join([str(c) for c in col if c]).strip() for col in df2.columns]

    # FORCE STRINGS
    df2 = df2.astype(str)

    # PREP VALUES
    values = [df2.columns.tolist()] + df2.values.tolist()

    # SEND TO SHEET
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=sheet_name,
        valueInputOption="RAW",
        body={"values": values},
    ).execute()

# ---------------------- Local cache helpers ----------------------
# LOCAL_CACHE = "local_cache.json"

# def load_local_cache():
#     try:
#         with open(LOCAL_CACHE, "r") as f:
#             cache = json.load(f)
#         members = pd.DataFrame(cache.get("members", []))
#         settings = pd.DataFrame(cache.get("settings", []))
#         schedule = pd.DataFrame(cache.get("schedule", []))
#         return members, settings, schedule, True
#     except Exception:
#         return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), False


# def save_local_cache(members_df, settings_df, schedule_df):
#     cache = {
#         "members": members_df.to_dict(orient="records"),
#         "settings": settings_df.to_dict(orient="records"),
#         "schedule": schedule_df.to_dict(orient="records"),
#     }
#     with open(LOCAL_CACHE, "w") as f:
#         json.dump(cache, f, indent=2)

LOCAL_CACHE = "local_cache.xlsx"

def load_local_cache(path="local_cache.xlsx"):
    if not os.path.exists(path):
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    try:
        xl = pd.ExcelFile(path)
        members = xl.parse("members") if "members" in xl.sheet_names else pd.DataFrame()
        settings = xl.parse("settings") if "settings" in xl.sheet_names else pd.DataFrame()
        schedule = xl.parse("schedule") if "schedule" in xl.sheet_names else pd.DataFrame()
        status = xl.parse("status") if "status" in xl.sheet_names else pd.DataFrame()
        return members, settings, schedule, status, True
    except:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), False



def save_local_cache_sheetname(df, sheet_name, path="local_cache.xlsx"):
# Ensure clean DataFrames
    if not isinstance(df, pd.DataFrame):
        df_save = pd.DataFrame()
    else:
        df_save = df
    
    with pd.ExcelWriter(path, engine="openpyxl") as writer: 
        if sheet_name=="schedule":
            if isinstance(df_save.columns, pd.MultiIndex):
                df_save.columns = ["_".join([str(c) for c in col]).strip() for col in df_save.columns]
        df_save.to_excel(writer, sheet_name=sheet_name, index=False)  

def save_local_cache(members, settings, schedule, path="local_cache.xlsx"):
    """Save all tables as an Excel workbook (no JSON)."""

    save_local_cache_sheetname(members, "members", path)
    save_local_cache_sheetname(settings, "settings", path)
    save_local_cache_sheetname(schedule, "schedule", path)
# Ensure clean DataFrames
    # if not isinstance(members, pd.DataFrame):
    #     members = pd.DataFrame()
    # if not isinstance(settings, pd.DataFrame):
    #     settings = pd.DataFrame()
    # if not isinstance(schedule, pd.DataFrame):
    #     schedule = pd.DataFrame()
    # if not isinstance(status, pd.DataFrame):
    #     status = pd.DataFrame()
    
    # with pd.ExcelWriter(path, engine="openpyxl") as writer: 
    #     members.to_excel(writer, sheet_name="members", index=False)
    #     settings.to_excel(writer, sheet_name="settings", index=False)
    #     #schedule.to_excel(writer, sheet_name="schedule", index=False)
    #     # Flatten MultiIndex columns if present
    #     if isinstance(schedule.columns, pd.MultiIndex):
    #         schedule.columns = ["_".join([str(c) for c in col]).strip() for col in schedule.columns]
    #     schedule.to_excel(writer, sheet_name="schedule", index=False)


# ---------------------- App load logic ----------------------

def try_load_all():
    """Try loading members/settings/schedule from Google Sheets. If any fail, load local cache instead."""
    sheets = st.secrets.get("google_sheets") or {}
    members_sheet = sheets.get("members_sheet", "members")
    settings_sheet = sheets.get("settings_sheet", "settings")
    schedule_sheet = sheets.get("schedule_sheet", "schedule")
    status_sheet = sheets.get("status_sheet", "schedule")
    # Attempt Google load
    try:
        members = load_google_sheet(members_sheet)
        settings = load_google_sheet(settings_sheet)
        schedule = load_google_sheet(schedule_sheet)
        status = load_google_sheet(status_sheet)
        # success
        return True, members, settings, schedule, status
    except Exception as e:
        # Fall back to local cache
        members, settings, schedule, status, loaded = load_local_cache()
        return False, members, settings, schedule, status

# ---------------------- Scheduling algorithm ----------------------

# def generate_schedule(players, num_courts, num_rounds, tries=300):
#     """
#     players: list of dicts with keys name, level, gender, active
#     returns: rounds list where each round is list of groups (each group is 4 names)
#     """
#     player_names = [p['name'] for p in players]
#     n = len(player_names)
#     per_round = num_courts * 4
#     if n < 4:
#         return []

#     games_count = Counter({name: 0 for name in player_names})
#     partner_count = defaultdict(Counter)
#     opponent_count = defaultdict(Counter)

#     rounds = []

#     def score(groups):
#         s = 0
#         for g in groups:
#             for p in g:
#                 s -= games_count[p] * 2.0
#         for g in groups:
#             a,b,c,d = g
#             s -= partner_count[a][b] * 8
#             s -= partner_count[c][d] * 8
#             for x in [a,b]:
#                 for y in [c,d]:
#                     s -= opponent_count[x][y] * 4
#         return s

#     # Pre-shuffle initial order to avoid deterministic results
#     for r in range(num_rounds):
#         best = None
#         best_score = -1e12
#         for t in range(tries):
#             pool = player_names.copy()
#             # bias selection towards those with fewer games
#             pool.sort(key=lambda x: games_count[x])
#             random.shuffle(pool)
#             selected = pool[:min(per_round, len(pool))]
#             random.shuffle(selected)
#             groups = []
#             while len(selected) >= 4 and len(groups) < num_courts:
#                 # pair low with high to mix
#                 p1 = selected.pop(0)
#                 p2 = selected.pop(0)
#                 p3 = selected.pop(-1)
#                 p4 = selected.pop(-1)
#                 groups.append([p1,p2,p3,p4])
#             if len(groups) < num_courts:
#                 # can't fill all courts, skip
#                 continue
#             sc = score(groups)
#             if sc > best_score:
#                 best_score = sc
#                 best = groups
#         if best is None:
#             break
#         # commit
#         for g in best:
#             a,b,c,d = g
#             games_count[a] += 1
#             games_count[b] += 1
#             games_count[c] += 1
#             games_count[d] += 1
#             partner_count[a][b] += 1
#             partner_count[b][a] += 1
#             partner_count[c][d] += 1
#             partner_count[d][c] += 1
#             for x in [a,b]:
#                 for y in [c,d]:
#                     opponent_count[x][y] += 1
#                     opponent_count[y][x] += 1
#         rounds.append(best)
#     return rounds




def generate_schedule_mixing_rule(players, num_courts, num_rounds,
                                  mixing="Random",
                                  repeat_rule="Minimize",
                                  tries=300):
    """
    players: list of dicts, with keys:
        - name
        - level (1–10)
        - gender ("M" / "F")
        - active ("Y" / "N")

    mixing: one of ["Random", "Level-based", "Minimize repeats"]
    repeat_rule: ["Avoid repeats","Minimize","No rule"]

    Returns:
        rounds: list of rounds (each round = list of groups of 4 names)
        games_df: pandas DataFrame columns ["name","number_of_game"]
    """

    # Filter only active players
    active_players = [p for p in players if p["active"].upper() == "Y"]

    player_names = [p["name"] for p in active_players]
    if len(player_names) < 4:
        return [], pd.DataFrame(columns=["name", "number_of_game"])

    # Quick maps for attributes
    level_map  = {p["name"]: p["level"] for p in active_players}
    gender_map = {p["name"]: p["gender"] for p in active_players}

    n = len(player_names)
    per_round = num_courts * 4

    # Tracking
    games_count = Counter({name: 0 for name in player_names})
    partner_count = defaultdict(Counter)
    opponent_count = defaultdict(Counter)

    rounds = []

    # Weight rules
    REPEAT_WEIGHTS = {
        "Avoid repeats": 12,
        "Minimize": 6,
        "No rule": 0
    }
    print(type(repeat_rule))
    repeat_weight = REPEAT_WEIGHTS.get(repeat_rule, 6)

    def score(groups):
        """
        Higher score = better schedule.
        """
        s = 0

        # Fair play count (players with fewer games get boosted)
        for g in groups:
            for p in g:
                s -= games_count[p] * 2.0

        # Partner and opponent repeat penalties
        for g in groups:
            a, b, c, d = g

            # partner repeats
            s -= partner_count[a][b] * repeat_weight
            s -= partner_count[c][d] * repeat_weight

            # opponent repeats
            for x in (a, b):
                for y in (c, d):
                    s -= opponent_count[x][y] * (repeat_weight / 2)

            # Level-based mixing bonus (if enabled)
            if mixing == "Level-based":
                levels = [level_map[p] for p in g]
                level_std = pd.Series(levels).std()
                s += (10 - level_std)  # smaller spread => more balanced

            # Minimize repeats mixing rule
            if mixing == "Minimize repeats":
                # Strongly reward groups with fewer historical pairings
                pair_penalty = (
                    partner_count[a][b] +
                    partner_count[c][d] +
                    opponent_count[a][c] + opponent_count[a][d] +
                    opponent_count[b][c] + opponent_count[b][d]
                )
                s -= pair_penalty * 4

        return s

    # ======================
    # Round generation loop
    # ======================
    for r in range(num_rounds):

        best = None
        best_score = -1e18

        for _ in range(tries):

            # Start fresh
            pool = player_names.copy()

            # Bias toward players with fewer games
            pool.sort(key=lambda x: games_count[x])
            random.shuffle(pool)

            # If level-based sorting enabled
            if mixing == "Level-based":
                pool.sort(key=lambda x: level_map[x])

            selected = pool[:min(per_round, len(pool))]
            random.shuffle(selected)

            groups = []

            while len(selected) >= 4 and len(groups) < num_courts:
                if mixing == "Random":
                    g = [selected.pop(), selected.pop(), selected.pop(), selected.pop()]

                elif mixing == "Level-based":
                    # pick low/mid/high to blend skill levels
                    selected.sort(key=lambda x: level_map[x])
                    g = [
                        selected.pop(0),   # lowest
                        selected.pop(-1),  # highest
                        selected.pop(len(selected)//2),  # mid
                        selected.pop(0 if len(selected)>0 else -1)
                    ]

                elif mixing == "Minimize repeats":
                    # Pick player with lowest games → pair with partners with least repeats
                    p1 = selected.pop(0)
                    partner_candidates = sorted(
                        selected,
                        key=lambda x: partner_count[p1][x] + opponent_count[p1][x]
                    )
                    p2 = partner_candidates[0]
                    selected.remove(p2)

                    # Opponents: least repeats with p1 & p2
                    opponent_candidates = sorted(
                        selected,
                        key=lambda x: opponent_count[p1][x] + opponent_count[p2][x]
                    )
                    p3 = opponent_candidates[0]
                    selected.remove(p3)
                    p4 = selected.pop(0)

                    g = [p1, p2, p3, p4]

                groups.append(g)

            if len(groups) < num_courts:
                continue

            sc = score(groups)

            if sc > best_score:
                best_score = sc
                best = groups

        if best is None:
            break

        # Commit results
        for g in best:
            a, b, c, d = g
            for p in g:
                games_count[p] += 1
            partner_count[a][b] += 1
            partner_count[b][a] += 1
            partner_count[c][d] += 1
            partner_count[d][c] += 1
            for x in (a, b):
                for y in (c, d):
                    opponent_count[x][y] += 1
                    opponent_count[y][x] += 1

        rounds.append(best)

    # Convert to DataFrame
    games_df = pd.DataFrame([
        {"name": name, "number_of_game": games_count[name]}
        for name in player_names
    ])

    return rounds, games_df


def generate_schedule(players, num_courts, num_rounds, tries=300):
    """
    players: list of dicts with keys name, level, gender, active
    returns: 
        rounds: list of rounds (each round is a list of groups of 4 names)
        games_df: pandas DataFrame with columns ["name", "number_of_game"]
    """
    player_names = [p['name'] for p in players]
    n = len(player_names)
    per_round = num_courts * 4
    if n < 4:
        return [], pd.DataFrame(columns=["name", "number_of_game"])

    games_count = Counter({name: 0 for name in player_names})
    partner_count = defaultdict(Counter)
    opponent_count = defaultdict(Counter)

    rounds = []

    def score(groups):
        s = 0
        for g in groups:
            for p in g:
                s -= games_count[p] * 2.0
        for g in groups:
            a,b,c,d = g
            s -= partner_count[a][b] * 8
            s -= partner_count[c][d] * 8
            for x in [a,b]:
                for y in [c,d]:
                    s -= opponent_count[x][y] * 4
        return s

    # Pre-shuffle initial order to avoid deterministic results
    for r in range(num_rounds):
        best = None
        best_score = -1e12
        for t in range(tries):
            pool = player_names.copy()
            # bias selection towards those with fewer games
            pool.sort(key=lambda x: games_count[x])
            random.shuffle(pool)
            selected = pool[:min(per_round, len(pool))]
            random.shuffle(selected)
            groups = []
            while len(selected) >= 4 and len(groups) < num_courts:
                # pair low with high to mix
                p1 = selected.pop(0)
                p2 = selected.pop(0)
                p3 = selected.pop(-1)
                p4 = selected.pop(-1)
                groups.append([p1,p2,p3,p4])
            if len(groups) < num_courts:
                # can't fill all courts, skip
                continue
            sc = score(groups)
            if sc > best_score:
                best_score = sc
                best = groups
        if best is None:
            break
        # commit
        for g in best:
            a,b,c,d = g
            games_count[a] += 1
            games_count[b] += 1
            games_count[c] += 1
            games_count[d] += 1
            partner_count[a][b] += 1
            partner_count[b][a] += 1
            partner_count[c][d] += 1
            partner_count[d][c] += 1
            for x in [a,b]:
                for y in [c,d]:
                    opponent_count[x][y] += 1
                    opponent_count[y][x] += 1
        rounds.append(best)

    # Convert games_count to a DataFrame
    games_df = pd.DataFrame([
        {"name": name, "number_of_game": games_count[name]}
        for name in player_names
    ])

    return rounds, games_df


# ---------------------- Helpers for display/export ----------------------

def schedule_to_dataframe(rounds, num_courts):
    rows = []
    for r_idx, groups in enumerate(rounds):
        row = {}
        for c in range(num_courts):
            if c < len(groups):
                team1 = f"{groups[c][0]} / {groups[c][1]}"
                team2 = f"{groups[c][2]} / {groups[c][3]}"
            else:
                team1 = ""
                team2 = ""
            row[(f"Court {c+1}", "Team 1")] = team1
            row[(f"Court {c+1}", "Team 2")] = team2
        rows.append(row)
    index = [f"Round {i+1}" for i in range(len(rows))]
    df = pd.DataFrame(rows, index=index)
    if not df.empty:
        df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


def to_excel_bytes(df):
    output = BytesIO()
    if isinstance(df.columns, pd.MultiIndex):
        # pandas will handle MultiIndex in Excel
        pass
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="schedule")
    return output.getvalue()

# ---------------------- Streamlit App ----------------------

st.set_page_config(page_title="Pickleball Scheduler", layout="wide")
st.title("🏓Elite Group Pickleball Scheduler")

# Try to load data once at startup
if "loaded_once" not in st.session_state:
    online, members_df, settings_df, schedule_df, status = try_load_all()
    st.session_state.online = online
    st.session_state.members_df = members_df
    st.session_state.settings_df = settings_df
    st.session_state.schedule_df = schedule_df    
    st.session_state.game_count_df = status
    st.session_state.loaded_once = True
else:
    online = st.session_state.online
    members_df = st.session_state.members_df
    settings_df = st.session_state.settings_df
    schedule_df = st.session_state.schedule_df
    game_count_df = st.session_state.game_count_df

# Sidebar: show mode and allow retry
st.sidebar.header("Connection")
if st.session_state.online:
    st.sidebar.success("Online — using Google Sheet data")
else:
    st.sidebar.warning("Offline — using local cache")

# Allow user to force a reload attempt
if st.sidebar.button("Retry Google Sheets connection"):
    try:
        online2, members2, settings2, schedule2, status2 = try_load_all()
        st.session_state.online = online2
        st.session_state.members_df = members2
        st.session_state.settings_df = settings2
        st.session_state.schedule_df = schedule2
        st.session_state.game_count_df = status2
        st.experimental_rerun()
    except Exception as e:
        st.sidebar.error(f"Retry failed: {e}")

# Tabs
#tab1, tab2, tab3 = st.tabs(["Schedule", "Members", "Settings"])

schedule_tab, members_tab, settings_tab, status_tab = st.tabs([
    "📅 Schedule", "🧑 Members", "⚙️ Settings", "ℹ️ Status"  
])

# ---------------------- Settings Tab ----------------------
with settings_tab:
    st.header("App Settings")
    # default settings
    default = {
        "number_of_courts": 2,
        "game_duration": 12,
        "total_session": 132,
        "mixing_rule": "Minimize repeats",
        "repeat_partner_rule": "Minimize",
    }
    if st.session_state.settings_df is None or st.session_state.settings_df.empty:
        settings = default.copy()
    else:
        # read first row of settings sheet (key/value format or wide format)
        try:
            srow = st.session_state.settings_df.iloc[0].to_dict()
            # merge
            settings = {**default, **{k: srow.get(k, default[k]) for k in default}}
        except Exception:
            settings = default.copy()

    nc = st.number_input("Number of Courts", min_value=1, max_value=20, value=int(settings.get("number_of_courts", 2)))
    gd = st.number_input("Game Duration (min)", min_value=5, max_value=60, value=int(settings.get("game_duration", 12)))
    ts = st.number_input("Total Session Time (min)", min_value=20, max_value=600, value=int(settings.get("total_session", 132)))
    
    mixing = st.selectbox("Mixing Rule", options=mixing_options, index=mixing_options.index(settings.get("mixing_rule", "Minimize repeats")))
    repeat_rule = st.selectbox("Repeat Partner Rule", options=repeat_options, index=repeat_options .index(settings.get("repeat_partner_rule", "Minimize")))
    #print(f"From the setting tab mixing: {mixing}, repeate rule: {repeat_rule}")
    s_df = pd.DataFrame([{"number_of_courts": nc, "game_duration": gd, "total_session": ts, "mixing_rule": mixing, "repeat_partner_rule": repeat_rule}])
   
    st.session_state.settings_df = s_df
    #mixing2 = st.session_state.settings_df["mixing_rule"].iloc[0]
    #repeat_rule2 = st.session_state.settings_df["repeat_partner_rule"].iloc[0]
    #print(f"From the schedule tab mixing: {mixing2}, repeate rule: {repeat_rule2}")
    
    if st.button("Save Settings"):
        # store settings in settings_df (single-row)        
        #save_local_cache(st.session_state.members_df or pd.DataFrame(), s_df, st.session_state.schedule_df or pd.DataFrame())
        #save_local_cache(st.session_state.members_df, s_df, st.session_state.schedule_df)
        #save_local_cache(st.session_state.members_df if isinstance(st.session_state.members_df, pd.DataFrame) else pd.DataFrame(), s_df if isinstance(s_df, pd.DataFrame) else pd.DataFrame(), st.session_state.schedule_df if isinstance(st.session_state.schedule_df, pd.DataFrame) else pd.DataFrame())

        if st.session_state.online:
            try:
                save_google_sheet(st.secrets["google_sheets"].get("settings_sheet", "settings"), s_df)
                st.success("Settings saved to Google Sheet")
            except Exception as e:
                st.error(f"Failed to save settings to Google Sheet: {e}")
        else:
            save_local_cache(st.session_state.members_df, s_df, st.session_state.schedule_df)
            st.success("Settings saved locally")

# ---------------------- Members Tab ----------------------
with members_tab:
    st.header("Members")
    # Ensure members_df has expected columns
    if st.session_state.members_df is None or st.session_state.members_df.empty:
        members = pd.DataFrame(columns=["name","level","gender","active"])
    else:
        members = st.session_state.members_df.copy()
        # normalize column names (lowercase) support both Name/Skill and name/level
        cols = [c.lower() for c in members.columns]
        members.columns = cols
        # ensure expected columns
        for col in ["name","level","gender","active"]:
            if col not in members.columns:
                members[col] = ""
        members = members[["name","level","gender","active"]]
    # Provide editable data editor
    edited = st.data_editor(members.rename(columns=str.title), num_rows="dynamic")
    # Normalize back to lowercase columns
    edited.columns = [c.lower() for c in edited.columns]
    st.session_state.members_df = edited

    # Save locally + to Google if online
    if st.button("Save Members"):
        #save_local_cache(st.session_state.members_df, st.session_state.settings_df or pd.DataFrame(), st.session_state.schedule_df or pd.DataFrame())
        #save_local_cache(st.session_state.members_df, st.session_state.settings_df, st.session_state.schedule_df )
        #save_local_cache(st.session_state.members_df if isinstance(st.session_state.members_df, pd.DataFrame) else pd.DataFrame(), st.session_state.settings_df if isinstance(st.session_state.settings_df, pd.DataFrame) else pd.DataFrame(), st.session_state.schedule_df if isinstance(st.session_state.schedule_df, pd.DataFrame) else pd.DataFrame())
        

        if st.session_state.online:
            try:
                save_google_sheet(st.secrets["google_sheets"].get("members_sheet", "members"), st.session_state.members_df)
                st.success("Members saved to Google Sheet")
            except Exception as e:
                st.error(f"Failed to save members to Google Sheet: {e}")
        else:
            save_local_cache(st.session_state.members_df, st.session_state.settings_df, st.session_state.schedule_df )
            st.success("Members saved locally")

# ---------------------- Schedule Tab ----------------------
with schedule_tab:
    st.header("Schedule Generator")
    # Filters
    members_for_filters = st.session_state.members_df if st.session_state.members_df is not None else pd.DataFrame()
    # normalize
    if not members_for_filters.empty:
        mf = members_for_filters.copy()
        cols = [c.lower() for c in mf.columns]
        mf.columns = cols
    else:
        mf = pd.DataFrame(columns=["name","level","gender","active"])

    # Default filters
    #level_min, level_max = st.slider("Level range", 1, 10, (1,10))
    #genders = st.multiselect("Gender", options=["F","M"], default=["F","M"])
    #active_only = st.checkbox("Active only", value=True)

    # Settings quick inputs (read defaults from settings if available)
    #default_nc = int(st.session_state.settings_df.iloc[0].get("number_of_courts", 2)) if (st.session_state.settings_df is not None and not st.session_state.settings_df.empty) else 2
    #default_gd = int(st.session_state.settings_df.iloc[0].get("game_duration", 12)) if (st.session_state.settings_df is not None and not st.session_state.settings_df.empty) else 12
    #default_ts = int(st.session_state.settings_df.iloc[0].get("total_session", 132)) if (st.session_state.settings_df is not None and not st.session_state.settings_df.empty) else 132

    # num_courts = st.number_input("Number of courts", min_value=1, max_value=20, value=default_nc)
    # game_duration = st.number_input("Game duration (min)", min_value=5, max_value=60, value=default_gd)
    # total_session = st.number_input("Total session time (min)", min_value=20, max_value=600, value=default_ts)
    # num_rounds = st.number_input("Number of rounds", min_value=1, value=math.floor(total_session / game_duration))    

    num_courts = st.session_state.settings_df["number_of_courts"].iloc[0]
    game_duration = st.session_state.settings_df["game_duration"].iloc[0]
    total_session = st.session_state.settings_df["total_session"].iloc[0]
    mixing = st.session_state.settings_df["mixing_rule"].iloc[0]
    repeat_rule = st.session_state.settings_df["repeat_partner_rule"].iloc[0]

    #print(f"From the schedule tab mixing: {mixing}, repeate rule: {repeat_rule}")
    num_rounds = math.floor(total_session / game_duration)

    # Build player list based on filters
    candidates = mf.copy()
    # coerce level to int if needed
    try:
        candidates['level'] = candidates['level'].astype(int)
    except Exception:
        candidates['level'] = pd.to_numeric(candidates['level'], errors='coerce').fillna(0).astype(int)
    
    level_min = 1
    level_max = 10
    genders = ["F","M"]

    candidates = candidates[candidates['active'].str.upper().isin(['Y','YES','TRUE'])]
    candidates = candidates[(candidates['level'] >= level_min) & (candidates['level'] <= level_max)]
    candidates = candidates[candidates['gender'].isin(genders)]   

    st.write(f"Players considered: {len(candidates)}")

    generate_click = st.button("Generate Schedule")
    if generate_click:
        player_dicts = candidates.rename(columns=str.lower).to_dict(orient='records')
        # Ensure 'name' key exists
        for p in player_dicts:
            if 'name' not in p and 'Name' in p:
                p['name'] = p.get('Name')
        # sanitize names
        for p in player_dicts:
            p['name'] = str(p.get('name','')).strip()
        #rounds, game_count_df = generate_schedule(player_dicts, int(num_courts), int(num_rounds), tries=400)
        rounds, game_count_df = generate_schedule_mixing_rule(player_dicts, int(num_courts), int(num_rounds), mixing, repeat_rule, tries=400)
        if not rounds:
            st.warning("Unable to generate schedule. Not enough players? Try reducing courts or rounds.")
        else:
            sched_df = schedule_to_dataframe(rounds, int(num_courts))
            st.session_state.schedule_df = sched_df.reset_index()
            st.session_state.game_count_df = game_count_df
            # Display nicely
            st.subheader("Generated Schedule")
            st.dataframe(sched_df)
            # Save to local cache and to Google if online
            #save_local_cache(st.session_state.members_df or pd.DataFrame(), st.session_state.settings_df or pd.DataFrame(), st.session_state.schedule_df or pd.DataFrame())
            #save_local_cache(st.session_state.members_df, st.session_state.settings_df, st.session_state.schedule_df)
            #save_local_cache(st.session_state.members_df if isinstance(st.session_state.members_df, pd.DataFrame) else pd.DataFrame(), st.session_state.settings_df if isinstance(st.session_state.settings_df, pd.DataFrame) else pd.DataFrame(), st.session_state.schedule_df if isinstance(st.session_state.schedule_df, pd.DataFrame) else pd.DataFrame())

            if st.session_state.online:
                try:
                    # schedule sheet: save flattened df
                    flat = st.session_state.schedule_df.copy()
                    save_google_sheet(st.secrets["google_sheets"].get("schedule_sheet", "schedule"), flat)
                    st.success("Schedule saved to Google Sheet")
                except Exception as e:
                    st.error(f"Failed to save schedule to Google Sheet: {e}")
            else:
                save_local_cache(st.session_state.members_df, st.session_state.settings_df, st.session_state.schedule_df)
                st.success("Schedule saved to local cache")

            # Export to Excel
            excel_bytes = to_excel_bytes(sched_df)
            st.download_button("Download Schedule as Excel", data=excel_bytes, file_name="schedule.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # If schedule exists from earlier load, show it
    if st.session_state.schedule_df is not None and not (st.session_state.schedule_df.empty):
        try:
            # If stored as reset_index earlier, try to convert back to multiindex view for display
            df_display = st.session_state.schedule_df.copy()
            if 'index' in df_display.columns:
                df_display = df_display.set_index('index')
            # attempt to convert flat to MultiIndex if possible
            # display
            st.subheader("Current Schedule")
            st.dataframe(df_display)
        except Exception:
            st.dataframe(st.session_state.schedule_df)

with status_tab:
    st.header("Status")
    if st.session_state.game_count_df is not None and not (st.session_state.game_count_df.empty):
        try:
            # If stored as reset_index earlier, try to convert back to multiindex view for display
            df_display = st.session_state.game_count_df.copy()
            if 'index' in df_display.columns:
                df_display = df_display.set_index('index')
            # attempt to convert flat to MultiIndex if possible
            # display
            st.subheader("Number of game assigned for players")
            st.dataframe(df_display)
        except Exception:
            st.dataframe(st.session_state.game_count_df)

    if st.session_state.online:
        try:
            # schedule sheet: save flattened df
            flat = st.session_state.game_count_df.copy()
            save_google_sheet(st.secrets["google_sheets"].get("status_sheet", "status"), flat)
            st.success("Status saved to Google Sheet")
        except Exception as e:
            st.error(f"Failed to save status to Google Sheet: {e}")
        else:
            save_local_cache_sheetname(st.session_state.game_count_df, "status")
            st.success("Status saved to local cache")
    

# ---------------------- Footer / Tips ----------------------
st.sidebar.markdown("---")
st.sidebar.write("Notes:")
st.sidebar.write("• The scheduler uses a heuristic that tries to minimize repeated partners/opponents and balance games played. (see the setting tab)")
st.sidebar.write("• For perfect fairness across many players and rounds, consider an optimization solver (slower).")
st.sidebar.write("• Web application is designed for private pickable group, contact to tungocnguyen@gmail.com for more information.")

