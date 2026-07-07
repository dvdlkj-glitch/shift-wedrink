"""
Shift Dashboard - auto scheduler
Constraint-aware greedy scheduler for a 3-location retail/service operation.
Locations: Aeropod, Lintas, Beverly. Region: Kota Kinabalu, Sabah (MYT, UTC+8).
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ----------------------------- helpers ------------------------------------
def load_config(path="data/week_config.json"):
    with open(path) as f:
        return json.load(f)


def week_dates(config):
    start = datetime.strptime(config["week_start"], "%Y-%m-%d").date()
    return [start + timedelta(days=i) for i in range(7)]


def emp_dict(employees_df):
    """Return {name: {type, locations:set, is_core, no_off_day}}."""
    d = {}
    for _, r in employees_df.iterrows():
        d[r["name"]] = {
            "type": r["type"],
            "locations": set(str(r["locations"]).split(";")),
            "is_core": str(r["is_core"]).strip().lower() in ("true", "1", "yes"),
            "no_off_day": str(r["no_off_day"]).strip().lower() in ("true", "1", "yes"),
        }
    return d


# ----------------------------- core scheduler -----------------------------
def generate_schedule(employees_df, config):
    emps = emp_dict(employees_df)
    dates = week_dates(config)
    fshifts = config["full_shifts"]
    pshifts = config["part_shifts"]
    required = config["coverage"]["full_required"]
    off_days = config.get("off_days", {})
    no_off = set(config.get("no_off_members", []))
    requests = config.get("shift_requests", [])
    part_avail = config.get("part_availability", {})

    full_names = [n for n, e in emps.items() if e["type"] == "full"]
    part_names = [n for n, e in emps.items() if e["type"] == "part"]

    # state
    assignments = []
    day_assigned = {d.isoformat(): set() for d in dates}   # names already working that day
    count = {n: 0 for n in emps}                            # total shifts assigned
    worked_dates = {n: set() for n in emps}                 # dates a person works

    def is_off(name, diso):
        return name in off_days.get(diso, [])

    def add(diso, dname, loc, shift, name, is_full, status, note=""):
        sd = fshifts[shift] if is_full else pshifts[shift]
        assignments.append({
            "date": diso, "day": dname, "location": loc, "shift": shift,
            "employee": name, "type": "full" if is_full else "part",
            "start": sd["start"], "end": sd["end"], "hours": sd["hours"],
            "status": status, "note": note,
            "clock_in": "", "clock_out": "",
        })
        day_assigned[diso].add(name)
        count[name] += 1
        worked_dates[name].add(diso)

    # --- 1. Pin hard shift requests -------------------------------------
    open_slots = {}  # (diso, loc, shift) -> remaining count
    for d in dates:
        diso = d.isoformat()
        for loc in config["locations"]:
            for shift, need in required.items():
                if need > 0:
                    open_slots[(diso, loc, shift)] = need

    def pick_location(name, diso, shift):
        """Choose an eligible location for name that still needs this shift."""
        elig = sorted([l for l in emps[name]["locations"] if l in config["locations"]])
        # prefer a location that still needs coverage
        needy = [l for l in elig if open_slots.get((diso, l, shift), 0) > 0]
        pool = needy or elig
        # deterministic: fewest eligible full-timers first handled globally; here just sort
        return pool[0] if pool else None

    for req in requests:
        if not req.get("hard"):
            continue
        name, diso, shift = req["name"], req.get("date"), req["shift"]
        if not diso or name not in emps:
            continue
        if is_off(name, diso) or name in day_assigned[diso]:
            continue
        loc = pick_location(name, diso, shift)
        if loc is None:
            continue
        add(diso, DAY_NAMES[dates.index(datetime.strptime(diso, "%Y-%m-%d").date())],
            loc, shift, name, emps[name]["type"] == "full", "pinned", req.get("note", ""))
        if open_slots.get((diso, loc, shift), 0) > 0:
            open_slots[(diso, loc, shift)] -= 1

    # --- 2. Fill remaining full-time coverage ---------------------------
    # scarcity: order locations by number of eligible full-timers (fewest first)
    loc_pool = {l: [n for n in full_names if l in emps[n]["locations"]] for l in config["locations"]}
    loc_order = sorted(config["locations"], key=lambda l: len(loc_pool[l]))

    soft_pref = {}  # name -> preferred shift (soft)
    for req in requests:
        if not req.get("hard") and req.get("shift"):
            soft_pref[req["name"]] = req["shift"]

    def score(name, diso, shift, loc):
        e = emps[name]
        s = count[name] * 10  # fairness: fewer assignments preferred
        # consecutive-day penalty (core members should not work 2 full days in a row)
        prev = (datetime.strptime(diso, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
        nxt = (datetime.strptime(diso, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
        if e["is_core"] and (prev in worked_dates[name] or nxt in worked_dates[name]):
            s += 25
        elif prev in worked_dates[name]:
            s += 3
        # honor soft preference
        if soft_pref.get(name) == shift:
            s -= 8
        # specialists (single-location) should stay on their location -> slight priority
        if len(e["locations"]) == 1:
            s -= 2
        return s

    for diso_loc_shift in sorted(open_slots.keys(),
                                 key=lambda k: (loc_order.index(k[1]), k[0], k[2])):
        diso, loc, shift = diso_loc_shift
        dname = DAY_NAMES[dates.index(datetime.strptime(diso, "%Y-%m-%d").date())]
        while open_slots[(diso, loc, shift)] > 0:
            cands = []
            for n in full_names:
                if loc not in emps[n]["locations"]:
                    continue
                if is_off(n, diso) or n in day_assigned[diso]:
                    continue
                # enforce weekly rest: non no-off members work at most 6 days
                if n not in no_off and len(worked_dates[n]) >= 6 and diso not in worked_dates[n]:
                    continue
                cands.append(n)
            if not cands:
                break  # cannot fill (flag as gap)
            best = min(cands, key=lambda n: (score(n, diso, shift, loc), n))
            add(diso, dname, loc, shift, best, True, "auto")
            open_slots[(diso, loc, shift)] -= 1

    # --- 3. Optional part-time support ----------------------------------
    def allowed_part_shifts(status):
        return {
            "anytime": ["Mid", "Night", "Morning"],
            "morning_only": ["Morning"],
            "evening_only": ["Night"],
            "no_morning": ["Mid", "Night"],
            "unavailable": [],
        }.get(status, [])

    for d in dates:
        diso = d.isoformat()
        dname = DAY_NAMES[dates.index(d)]
        for n in part_names:
            status = part_avail.get(n, {}).get(diso, "unavailable")
            shifts_ok = allowed_part_shifts(status)
            if not shifts_ok or n in day_assigned[diso]:
                continue
            elig = sorted([l for l in emps[n]["locations"] if l in config["locations"]])
            if not elig:
                continue
            shift = shifts_ok[0]
            loc = elig[0]
            add(diso, dname, loc, shift, n, False, "auto", f"PT avail: {status}")

    return assignments


def find_gaps(assignments, config):
    """Return list of (date, location, shift) full-time slots not fully covered."""
    required = config["coverage"]["full_required"]
    dates = week_dates(config)
    filled = {}
    for a in assignments:
        if a["type"] == "full":
            k = (a["date"], a["location"], a["shift"])
            filled[k] = filled.get(k, 0) + 1
    gaps = []
    for d in dates:
        diso = d.isoformat()
        for loc in config["locations"]:
            for shift, need in required.items():
                if need > 0 and filled.get((diso, loc, shift), 0) < need:
                    gaps.append((diso, loc, shift,
                                 need - filled.get((diso, loc, shift), 0)))
    return gaps


if __name__ == "__main__":
    import pandas as pd
    cfg = load_config()
    emp = pd.read_csv("data/employees.csv")
    sched = generate_schedule(emp, cfg)
    df = pd.DataFrame(sched)
    print(f"Total assignments: {len(df)}")
    print(f"Full-time: {(df['type']=='full').sum()}  Part-time: {(df['type']=='part').sum()}")
    print("\nShifts per employee:")
    print(df["employee"].value_counts().to_string())
    print("\nCoverage gaps:", find_gaps(sched, cfg))
    print("\nSample (Monday):")
    print(df[df["date"]=="2026-07-06"][["location","shift","employee","type","status"]].to_string(index=False))
