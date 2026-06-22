import streamlit as st
import pandas as pd
import numpy as np
import pulp
from datetime import datetime
from io import BytesIO

st.set_page_config(page_title="Combibrug Workforce Planner", layout="wide")
st.title("Combibrug Workforce Planning Tool")

DAYS = ["Monday","Tuesday","Wednesday","Thursday","Friday"]

def get_project_type(project):
    if project.startswith("Combibrug CC"):    return "CC"
    elif project.startswith("Combibrug BSC"): return "BSC"
    elif project.startswith("MDT"):           return "MDT"
    elif "MP" in project:                     return "Combiworld-MP"
    elif project.startswith("Combiworld"):    return "Combiworld"
    return ""

def parse_list(val):
    if pd.isna(val) or str(val).strip() == "": return []
    return [v.strip() for v in str(val).split(",") if v.strip()]

def parse_time(val):
    if pd.isna(val): return None
    try: return datetime.strptime(str(val)[:5], "%H:%M")
    except: return None

def calc_session_hours(start, end):
    try:
        s = datetime.strptime(str(start)[:5], "%H:%M")
        e = datetime.strptime(str(end)[:5],   "%H:%M")
        return (e - s).seconds / 3600
    except: return None

def times_conflict(s1, e1, s2, e2, buffer=1.0):
    if not all([s1, e1, s2, e2]): return False
    if s1 > s2: s1, e1, s2, e2 = s2, e2, s1, e1
    gap = (s2 - e1).seconds / 3600
    return gap < buffer

@st.cache_data
def load_staff(file_bytes):
    staff = pd.read_excel(BytesIO(file_bytes))
    staff["employee_id"]       = pd.to_numeric(staff["employee_id"], errors="coerce").astype("Int64")
    staff["weekly_hours"]      = pd.to_numeric(staff["weekly_hours"], errors="coerce")
    staff["avg_hourly_rate"]   = pd.to_numeric(staff["avg_hourly_rate"], errors="coerce").fillna(50)
    staff["is_dreammaker"]     = staff["is_dreammaker"].astype(bool)
    staff["is_kantoor"]        = staff["is_kantoor"].astype(bool)
    staff["eligible_projects"] = staff["eligible_projects"].apply(parse_list)
    staff["available_days"]    = staff["available_days"].apply(
        lambda v: parse_list(v) if pd.notna(v) else DAYS)
    return staff

# ── Step 1: File uploads ─────────────────────────────────────
st.header("1. Upload input files")
col1, col2 = st.columns(2)
with col1: staff_file = st.file_uploader("Staff template (incl. hourly rate)", type=["xlsx"])
with col2: proj_file  = st.file_uploader("Project requirements",               type=["xlsx"])

if not (staff_file and proj_file):
    st.info("Upload both files to continue.")
    st.stop()

staff = load_staff(staff_file.read())

# ── How to use ───────────────────────────────────────────────
with st.expander("ℹ️ How to use this tool", expanded=False):
    st.markdown("""
**This tool generates an optimal weekly workforce assignment for Combibrug projects.**

**Input files:**
- **Staff file**: Contains employee information including contracted hours, eligibility, availability, and hourly rates.
- **Project requirements**: Contains the active projects for the planning week, including required hours, headcount, scheduled days, and session times.

**Steps:**
1. Upload both input files above.
2. Select the planning week.
3. Review and adjust employee availability if needed (e.g. for leave or illness).
4. Review and adjust project requirements for the upcoming week.
5. Check the feasibility warnings before running.
6. Set the freelancer hourly rate and run the optimization.
7. Download the results as an Excel file.

**Output tables:**
- **Table 1 — Daily schedule**: Shows which employee works on which project, on which day, and at what time.
- **Table 2 — Employee summary**: Shows total hours, projects, contract hours, and cost per employee.
- **Table 3 — Freelancer requirements**: Shows projects that could not be fully staffed internally, with estimated freelancer hours and cost.

**Tips:**
- If the model returns *Infeasible*, check the feasibility warnings and reduce the number of active projects or adjust availability.
- Freelancer cost in Table 3 is an estimate based on the hourly rate you set. Adjust it to reflect the actual market rate.
    """)

# ── Step 2: Select planning week ─────────────────────────────
st.header("2. Select planning week")
planning_week = st.text_input("Planning week (ISO format, e.g. 2025-W1)", value="2025-W1")

# ── Step 3: Edit availability ────────────────────────────────
st.header("3. Review & edit availability (hours/week)")
st.caption("Edit if needed (e.g. employee on leave this week).")
avail_df     = staff[["employee_id","weekly_hours"]].copy().set_index("employee_id")
avail_edited = st.data_editor(avail_df, use_container_width=True, num_rows="fixed")
A = {e: float(avail_edited.loc[e,"weekly_hours"])
     if e in avail_edited.index and pd.notna(avail_edited.loc[e,"weekly_hours"]) else 0.0
     for e in staff["employee_id"].tolist()}

# ── Step 4: Edit project requirements ───────────────────────
st.header("4. Review & edit project requirements")
st.caption("Only include projects active this week.")
df_proj_raw = pd.read_excel(BytesIO(proj_file.read()))
df_proj_raw = df_proj_raw[[c for c in df_proj_raw.columns
                            if not str(c).startswith("Unnamed")
                            and c.lower() != "notes"]]
proj_edited = st.data_editor(df_proj_raw, use_container_width=True, num_rows="dynamic")

# ── Step 5: Feasibility checks ───────────────────────────────
st.header("5. Feasibility checks")

proj_check = proj_edited.copy()
proj_check["hours_per_week"] = pd.to_numeric(proj_check["hours_per_week"], errors="coerce")
proj_check["headcount"]      = pd.to_numeric(proj_check["headcount"], errors="coerce").fillna(1)
proj_check["days_list"]      = proj_check["days"].apply(parse_list)
proj_check["row_demand"]     = proj_check["hours_per_week"] * proj_check["headcount"]
D_check      = proj_check.groupby("project")["row_demand"].sum().to_dict()
proj_days_ck = {p: list(set(d for days in proj_check[proj_check["project"]==p]["days_list"] for d in days))
                for p in D_check}

E_check  = staff["employee_id"].tolist()
role_ck  = dict(zip(staff["employee_id"], staff["role"].fillna("").str.lower()))
kant_ck  = dict(zip(staff["employee_id"], staff["is_kantoor"]))
dm_ck    = dict(zip(staff["employee_id"], staff["is_dreammaker"]))

PL_KW    = ["locatie leider","proj.l"]
pl_list  = [e for e in E_check if any(k in role_ck.get(e,"") for k in PL_KW) and not kant_ck.get(e,False)]
dm_list  = [e for e in E_check if dm_ck.get(e,False)]

bsc_ck    = [p for p in D_check if p.startswith("Combibrug BSC") and D_check[p]>0]
mdt_cw_ck = [p for p in D_check if get_project_type(p) in ("MDT","Combiworld","Combiworld-MP") and D_check[p]>0]

total_demand = sum(D_check.values())
total_avail  = sum(A.get(e,0) for e in E_check if not kant_ck.get(e,False))
pl_capacity  = len(pl_list) * 5

warnings, infos = [], []

if total_demand > total_avail:
    warnings.append(f"Total weekly demand ({total_demand:.0f}h) exceeds staff availability ({total_avail:.0f}h). Freelancers will be required.")
else:
    infos.append(f"Demand ({total_demand:.0f}h) vs availability ({total_avail:.0f}h): surplus of {total_avail-total_demand:.0f}h.")

if bsc_ck and not pl_list:
    warnings.append(f"No project leaders found. C5 constraint cannot be satisfied for {len(bsc_ck)} BSC projects.")
elif bsc_ck and len(bsc_ck) > pl_capacity:
    warnings.append(f"{len(bsc_ck)} BSC projects require a project leader, but {len(pl_list)} leaders can cover max {pl_capacity}. Consider reducing BSC projects.")
elif bsc_ck:
    infos.append(f"BSC project leader check: {len(pl_list)} leaders for {len(bsc_ck)} projects. OK.")

if mdt_cw_ck and not dm_list:
    warnings.append(f"No Dreammaker employees found. C4 constraint cannot be satisfied for {len(mdt_cw_ck)} MDT/Combiworld projects.")
elif mdt_cw_ck:
    infos.append(f"Dreammaker check: {len(dm_list)} Dreammakers for {len(mdt_cw_ck)} MDT/Combiworld projects. OK.")

no_days = [p for p in D_check if not proj_days_ck.get(p,[])]
if no_days:
    infos.append(f"{len(no_days)} projects have no day info — hours assigned based on weekly demand only.")

for w in warnings:
    st.warning(f"⚠️ {w}")
for i in infos:
    st.info(f"ℹ️ {i}")
if warnings:
    st.caption("These warnings may cause infeasibility. Review before running.")

# ── Step 6: Run optimization ─────────────────────────────────
st.header("6. Run optimization")

col_rate, col_btn = st.columns([2,3])
with col_rate:
    freelancer_rate = st.number_input(
        "Freelancer hourly rate (€)",
        min_value=1, max_value=500, value=200,
        help="Set the penalty rate for freelancer hours. A higher rate forces the model to maximize internal staffing."
    )

if st.button("Run MILP optimization", type="primary"):
    with st.spinner("Solving..."):

        proj_edited["hours_per_week"] = pd.to_numeric(proj_edited["hours_per_week"], errors="coerce")
        proj_edited["headcount"]      = pd.to_numeric(proj_edited["headcount"], errors="coerce").fillna(1)

        type_avg = proj_edited.groupby("funding_type")["hours_per_week"].mean()
        def fill_hours(row):
            if pd.notna(row["hours_per_week"]): return row["hours_per_week"]
            return round(type_avg.get(row["funding_type"], np.nan), 1)
        proj_edited["hours_per_week"]    = proj_edited.apply(fill_hours, axis=1)
        proj_edited["days_list"]         = proj_edited["days"].apply(parse_list)
        proj_edited["start_parsed"]      = proj_edited["start_time"].apply(parse_time)
        proj_edited["end_parsed"]        = proj_edited["end_time"].apply(parse_time)
        proj_edited["hours_per_session"] = proj_edited.apply(
            lambda r: calc_session_hours(r["start_time"], r["end_time"]), axis=1)
        proj_edited["row_demand"]        = proj_edited["hours_per_week"] * proj_edited["headcount"]

        D              = proj_edited.groupby("project")["row_demand"].sum().to_dict()
        proj_headcount = proj_edited.groupby("project")["headcount"].max().to_dict()

        project_schedule = {}
        for _, row in proj_edited.iterrows():
            p = row["project"]
            if p not in project_schedule: project_schedule[p] = []
            for day in row["days_list"]:
                h = row["hours_per_session"] or (row["hours_per_week"] / max(len(row["days_list"]),1))
                project_schedule[p].append({
                    "day"      : day,
                    "headcount": int(row["headcount"]),
                    "start"    : row["start_parsed"],
                    "end"      : row["end_parsed"],
                    "start_str": str(row["start_time"])[:5] if pd.notna(row["start_time"]) else "",
                    "end_str"  : str(row["end_time"])[:5]   if pd.notna(row["end_time"])   else "",
                    "hours"    : float(h) if h and not np.isnan(float(h)) else 0.0
                })

        project_days = {p: list(set(s["day"] for s in sch))
                        for p, sch in project_schedule.items()}

        E = staff["employee_id"].tolist()
        P = list(D.keys())

        role_map          = dict(zip(staff["employee_id"], staff["role"].fillna("").str.lower()))
        is_kantoor_map    = dict(zip(staff["employee_id"], staff["is_kantoor"]))
        is_dreammaker_map = dict(zip(staff["employee_id"], staff["is_dreammaker"]))
        avail_days_map    = dict(zip(staff["employee_id"], staff["available_days"]))
        c                 = dict(zip(staff["employee_id"], staff["avg_hourly_rate"].fillna(50)))
        max_daily         = {e: A.get(e,0) / 5 for e in E}

        def get_emp_avail_days(emp_id):
            v = avail_days_map.get(emp_id, DAYS)
            if isinstance(v, str): return [d.strip() for d in v.split(",")]
            return v if isinstance(v, list) else DAYS

        def get_elig_list(emp_id):
            v = staff.loc[staff["employee_id"]==emp_id, "eligible_projects"].values
            if len(v)==0: return []
            return v[0] if isinstance(v[0], list) else parse_list(str(v[0]))

        def is_eligible(emp_id, project):
            if is_kantoor_map.get(emp_id, False): return 0
            emp_elig = get_elig_list(emp_id)
            ft = get_project_type(project).upper().replace("-","")
            return 1 if any(ft.startswith(e.replace("-","")) or
                            e.replace("-","").startswith(ft) for e in emp_elig) else 0

        def has_day_overlap(emp_id, project):
            emp_days  = get_emp_avail_days(emp_id)
            proj_days = project_days.get(project, [])
            if not proj_days: return True
            return bool(set(emp_days) & set(proj_days))

        def get_hours_if_assigned(emp_id, project):
            emp_days = get_emp_avail_days(emp_id)
            slots    = project_schedule.get(project, [])
            if not slots:
                d  = D.get(project, 0)
                hc = proj_headcount.get(project, 1)
                val = d / max(1, hc) if (d and hc and not np.isnan(float(d))) else 0.0
                return float(val)
            total = 0.0
            for slot in slots:
                if slot["day"] in emp_days:
                    h = slot.get("hours", 0)
                    if h and not np.isnan(float(h)):
                        total += float(h)
            return total

        elig = {(e,p): is_eligible(e,p) * has_day_overlap(e,p) for e in E for p in P}

        dreammakers     = [e for e in E if is_dreammaker_map.get(e, False)]
        project_leaders = [e for e in E if any(k in str(role_map.get(e,"")) for k in PL_KW)]
        mdt_cw_projects = [p for p in P if get_project_type(p) in ("MDT","Combiworld","Combiworld-MP")]
        bsc_projects    = [p for p in P if get_project_type(p) == "BSC"]

        # Build conflict pairs (gap < 1 hour = conflict)
        conflicts_set = set()
        for day in DAYS:
            day_slots = [(p, sl["start"], sl["end"])
                         for p, sch in project_schedule.items()
                         for sl in sch if sl["day"] == day]
            for i in range(len(day_slots)):
                for j in range(i+1, len(day_slots)):
                    p1,s1,e1 = day_slots[i]; p2,s2,e2 = day_slots[j]
                    if p1 != p2 and times_conflict(s1,e1,s2,e2):
                        conflicts_set.add((day, min(p1,p2), max(p1,p2)))

        model = pulp.LpProblem("Combibrug", pulp.LpMinimize)

        y = {(e,p): pulp.LpVariable(f"y_{e}_{P.index(p)}", cat="Binary")
             for e in E for p in P if elig[(e,p)]==1 and D.get(p,0)>0}
        s = {p: pulp.LpVariable(f"s_{P.index(p)}", lowBound=0)
             for p in P if D.get(p,0)>0}

        model += (
            pulp.lpSum(c.get(e,0) * get_hours_if_assigned(e,p) * y[(e,p)] for (e,p) in y) +
            pulp.lpSum(freelancer_rate * s[p] for p in s)
        )

        for e in E:
            terms = [get_hours_if_assigned(e,p) * y[(e,p)] for p in P if (e,p) in y]
            if terms: model += pulp.lpSum(terms) <= A.get(e,0), f"C1_e{e}"

        for p in P:
            d = D.get(p,0)
            if d > 0:
                terms_p = [get_hours_if_assigned(e,p) * y[(e,p)] for e in E if (e,p) in y]
                if terms_p:
                    model += pulp.lpSum(terms_p) + s.get(p,0) >= d, f"C2_p{P.index(p)}"

        for e in E:
            mdh = max_daily.get(e, 8)
            for p in P:
                if (e,p) in y:
                    emp_days = get_emp_avail_days(e)
                    for slot in [sl for sl in project_schedule.get(p,[]) if sl["day"] in emp_days]:
                        if slot.get("hours",0) > mdh:
                            model += y[(e,p)] == 0, f"C3_e{e}_p{P.index(p)}"
                            break

        for p in mdt_cw_projects:
            if D.get(p,0) > 0:
                ay = [y[(e,p)] for e in E if (e,p) in y]
                if ay: model += pulp.lpSum(ay) >= 2, f"C4a_p{P.index(p)}"
                dy = [y[(e,p)] for e in dreammakers if (e,p) in y]
                if dy: model += pulp.lpSum(dy) >= 1, f"C4b_p{P.index(p)}"

        for p in bsc_projects:
            if D.get(p,0) > 0:
                pl_v = [y[(e,p)] for e in project_leaders if (e,p) in y]
                if pl_v: model += pulp.lpSum(pl_v) >= 1, f"C5_p{P.index(p)}"

        for day, p1, p2 in conflicts_set:
            for e in E:
                if (e,p1) in y and (e,p2) in y:
                    model += y[(e,p1)] + y[(e,p2)] <= 1, f"C6_e{e}_d{day}_p{P.index(p1)}vp{P.index(p2)}"

        model.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=300))
        status     = pulp.LpStatus[model.status]
        total_cost = pulp.value(model.objective)

        records = []
        for (e,p), var in y.items():
            val = pulp.value(var)
            if val and val > 0.5:
                emp_row = staff[staff["employee_id"]==e]
                hours   = get_hours_if_assigned(e, p)
                records.append({
                    "employee_id"            : e,
                    "role"                   : emp_row["role"].values[0] if len(emp_row)>0 else None,
                    "worker_type"            : emp_row["worker_type"].values[0] if len(emp_row)>0 else None,
                    "project"                : p,
                    "funding"                : get_project_type(p),
                    "hours"                  : round(hours,1),
                    "cost_eur"               : round(hours*c.get(e,0),2),
                    "contract_hours_per_week": emp_row["weekly_hours"].values[0] if len(emp_row)>0 else None
                })
        results = pd.DataFrame(records)

        fl_records = []
        for p, var in s.items():
            val = pulp.value(var)
            if val and val > 0.01:
                fl_records.append({
                    "project"         : p,
                    "funding"         : get_project_type(p),
                    "freelancer_hours": round(val,1),
                    "freelancer_cost_estimated": round(val*freelancer_rate,2)
                })
        freelancer_df = pd.DataFrame(fl_records)

        schedule_records = []
        for (e,p), var in y.items():
            val = pulp.value(var)
            if val and val > 0.5:
                slots    = project_schedule.get(p,[])
                emp_days = get_emp_avail_days(e)
                if slots:
                    for slot in slots:
                        if slot["day"] in emp_days:
                            schedule_records.append({
                                "day"        : slot["day"],
                                "start_time" : slot.get("start_str",""),
                                "end_time"   : slot.get("end_str",""),
                                "project"    : p,
                                "employee_id": e,
                                "hours"      : round(slot.get("hours",0),1)
                            })
                else:
                    schedule_records.append({
                        "day": "—", "start_time": "", "end_time": "",
                        "project": p, "employee_id": e,
                        "hours": round(get_hours_if_assigned(e,p),1)
                    })

        day_order   = {d:i for i,d in enumerate(DAYS)}
        schedule_df = pd.DataFrame(schedule_records)
        if not schedule_df.empty:
            schedule_df["day_order"] = schedule_df["day"].map(day_order)
            schedule_df = schedule_df.sort_values(
                ["day_order","start_time","project","employee_id"]).drop(columns=["day_order"])

    # ── Results ──────────────────────────────────────────────
    st.header("7. Results")
    st.subheader(f"Week: {planning_week}")

    internal_cost   = results["cost_eur"].sum()                        if not results.empty      else 0
    freelancer_cost = freelancer_df["freelancer_cost_estimated"].sum() if not freelancer_df.empty else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status",          status)
    c2.metric("Internal cost",   f"€{internal_cost:,.0f}")
    c3.metric("Freelancer cost", f"€{freelancer_cost:,.0f}")
    c4.metric("Total cost",      f"€{internal_cost+freelancer_cost:,.0f}")

    st.subheader("Table 1 — Daily schedule")
    if not schedule_df.empty:
        st.dataframe(schedule_df.reset_index(drop=True), use_container_width=True)
    else:
        st.warning("No schedule found.")

    st.subheader("Table 2 — Employee summary")
    if not results.empty:
        emp_summary = results.groupby("employee_id").agg(
            role                    = ("role", "first"),
            worker_type             = ("worker_type", "first"),
            projects                = ("project", lambda x: ", ".join(sorted(x.unique()))),
            total_hours             = ("hours", "sum"),
            contract_hours_per_week = ("contract_hours_per_week", "first"),
            total_cost_eur          = ("cost_eur", "sum")
        ).reset_index()
        emp_summary["total_hours"]    = emp_summary["total_hours"].round(1)
        emp_summary["total_cost_eur"] = emp_summary["total_cost_eur"].round(2)
        st.dataframe(emp_summary, use_container_width=True)
    else:
        st.warning("No assignments found.")

    st.subheader("Table 3 — Freelancer requirements")
    if not freelancer_df.empty:
        st.caption("You can edit the freelancer hourly rate below to update the cost estimate.")
        fl_rate_input = st.number_input(
            "Freelancer hourly rate for cost estimate (€)",
            min_value=1, max_value=500,
            value=int(freelancer_rate),
            key="fl_rate_display"
        )
        freelancer_df["freelancer_cost_estimated"] = (
            freelancer_df["freelancer_hours"] * fl_rate_input).round(2)
        st.dataframe(
            freelancer_df[["project","funding","freelancer_hours","freelancer_cost_estimated"]]
            .sort_values("project").reset_index(drop=True),
            use_container_width=True)
    else:
        st.success("No freelancers needed — all projects fully staffed by internal staff.")

    st.subheader("Download results")
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        if not schedule_df.empty:
            schedule_df.to_excel(writer, sheet_name="Daily Schedule",   index=False)
        if not results.empty:
            results.to_excel(writer,     sheet_name="Employee Summary", index=False)
        if not freelancer_df.empty:
            freelancer_df.to_excel(writer, sheet_name="Freelancer",     index=False)
        pd.DataFrame([{
            "planning_week"  : planning_week,
            "status"         : status,
            "internal_cost"  : internal_cost,
            "freelancer_cost": freelancer_cost,
            "total_cost"     : internal_cost + freelancer_cost
        }]).to_excel(writer, sheet_name="Summary", index=False)
    st.download_button(
        label="Download results (Excel)",
        data=out.getvalue(),
        file_name=f"planning_{planning_week}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )