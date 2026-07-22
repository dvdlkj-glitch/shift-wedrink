"""
WeDrink Sabah — Shift Dashboard
Auto + semi-auto staff scheduling for a 3-location operation in
Kota Kinabalu, Sabah (Malaysia, UTC+8 / Asia/Kuching).

Two views:
  * Overall (staff) — read-only weekly schedule; anyone can view / find own shifts.
  * Admin           — login required (default admin / wedrink2026, changeable in-app).

Click-to-assign: pick an employee and the Location dropdown only offers the branches
that employee is cleared to work. 6-12 July 2026 loaded as real test data.
"""
import os
import json
import math
import hashlib
import urllib.request
import urllib.error
from urllib.parse import quote, urlencode
from datetime import datetime, timedelta, time

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Kuching")
except Exception:
    TZ = None

import scheduler as sch

DATA_DIR = "data"
EMP_CSV = os.path.join(DATA_DIR, "employees.csv")
CFG_JSON = os.path.join(DATA_DIR, "week_config.json")
SCHED_CSV = os.path.join(DATA_DIR, "schedule.csv")
ADMIN_JSON = os.path.join(DATA_DIR, "admin.json")
SITES_JSON = os.path.join(DATA_DIR, "sites.json")
SETTINGS_JSON = os.path.join(DATA_DIR, "settings.json")

# Staff may check in this many minutes before their shift start (admin-adjustable
# in Setup). Late check-ins are always allowed and recorded with minutes late.
DEFAULT_EARLY_MIN = 15

# GPS geofence per branch. Coordinates are PLACEHOLDERS — an admin must stand at
# each branch and capture the real centre (Setup ▸ Branch check-in geofences).
# "configured": False disables staff check-in for that branch until it is set.
DEFAULT_SITES = {
    "Aeropod": {"lat": 5.9389, "lng": 116.0539, "radius_m": 20, "configured": False},
    "Lintas":  {"lat": 5.9631, "lng": 116.0736, "radius_m": 20, "configured": False},
    "Beverly": {"lat": 5.9436, "lng": 116.0895, "radius_m": 20, "configured": False},
}
# Reject a fix this coarse — it's a WiFi/cell estimate, not real GPS (anti-spoof).
MAX_ACCURACY_M = 150

BRAND_GREEN = "#1C4A42"
BRAND_GREEN2 = "#15382F"
BRAND_ORANGE = "#F08A24"
BRAND_TEAL = "#33BEC4"
BRAND_CREAM = "#F4EFE3"

LOC_DISPLAY = {
    "Aeropod": "WEDRINK Aeropod",
    "Lintas": "WEDRINK Lintas Plaza",
    "Beverly": "WEDRINK Beverly Hills",
}
LOC_COLORS = {"Aeropod": "#1CA7A0", "Lintas": "#4C9A2A", "Beverly": "#E0712F"}
SHIFT_ICON = {"Morning": "☀️", "Mid": "🌤️", "Night": "🌙"}
LOC_EMOJI = {"Aeropod": "✈️", "Lintas": "🛍️", "Beverly": "🏙️"}

st.set_page_config(page_title="WeDrink Sabah — Shift Dashboard",
                   page_icon="🧋", layout="wide")

# Build marker — bump when debugging deploys to confirm which code Cloud runs.
APP_BUILD = "b20-2026-07-15"

DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PW = "wedrink2026"


def _hash(pw):
    return hashlib.sha256(("wedrink$sabah$salt$" + pw).encode()).hexdigest()


def load_admin():
    if os.path.exists(ADMIN_JSON):
        try:
            return json.load(open(ADMIN_JSON))
        except Exception:
            pass
    a = {"user": DEFAULT_ADMIN_USER, "hash": _hash(DEFAULT_ADMIN_PW)}
    try:
        json.dump(a, open(ADMIN_JSON, "w"))
    except Exception:
        pass
    return a


def save_admin(a):
    json.dump(a, open(ADMIN_JSON, "w"))


def verify_admin(user, pw):
    a = load_admin()
    return user.strip() == a["user"] and _hash(pw) == a["hash"]


@st.cache_data
def load_employees():
    return pd.read_csv(EMP_CSV)


def load_config():
    with open(CFG_JSON) as f:
        return json.load(f)


def now_myt():
    return datetime.now(TZ) if TZ else datetime.now()


SCHED_COLS = ["date", "day", "location", "shift", "employee", "type",
              "start", "end", "hours", "status", "note", "clock_in", "clock_out",
              "ci_lat", "ci_lng", "ci_acc", "ci_dist", "ci_method"]


def _load_schedule_csv():
    if os.path.exists(SCHED_CSV):
        df = pd.read_csv(SCHED_CSV, dtype=str).fillna("")
        for c in SCHED_COLS:
            if c not in df.columns:
                df[c] = ""
        df["hours"] = pd.to_numeric(df["hours"], errors="coerce").fillna(0)
        return df[SCHED_COLS]
    return pd.DataFrame(columns=SCHED_COLS)


def _save_schedule_csv(df):
    df.to_csv(SCHED_CSV, index=False)


def load_schedule():
    """Roster from Supabase when configured (durable across redeploys), else the
    local CSV. On the first DB run the committed CSV seeds the shifts table."""
    if db_enabled():
        df = db_fetch_shifts()
        if df is None:                 # transport error — use CSV for this run
            return _load_schedule_csv()
        if df.empty:                   # empty table — migrate the committed CSV in
            seed = _load_schedule_csv()
            if not seed.empty:
                db_replace_shifts(seed)
                return seed
        return df
    return _load_schedule_csv()


def save_schedule(df):
    """Persist the whole roster — to Supabase (durable) when configured, else CSV."""
    if db_enabled():
        db_replace_shifts(df)
    else:
        _save_schedule_csv(df)


def actual_hours(ci, co):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            a = datetime.strptime(str(ci).strip(), fmt)
            b = datetime.strptime(str(co).strip(), fmt)
            if b < a:
                b += timedelta(days=1)
            return round((b - a).total_seconds() / 3600, 2)
        except Exception:
            continue
    return None


# ===================== GPS CHECK-IN =====================
def load_sites():
    """Branch geofences {loc: {lat,lng,radius_m,configured}}. Auto-seeds any
    location missing from sites.json so new branches never break check-in."""
    d = {}
    if os.path.exists(SITES_JSON):
        try:
            d = json.load(open(SITES_JSON))
        except Exception:
            d = {}
    changed = False
    for loc in config["locations"]:
        if loc not in d:
            d[loc] = dict(DEFAULT_SITES.get(
                loc, {"lat": 5.98, "lng": 116.07, "radius_m": 20, "configured": False}))
            changed = True
    if changed:
        try:
            save_sites(d)
        except Exception:
            pass
    return d


def save_sites(d):
    json.dump(d, open(SITES_JSON, "w"), indent=2)


def haversine_m(lat1, lng1, lat2, lng2):
    """Great-circle distance in metres."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def load_settings():
    d = {"early_min": DEFAULT_EARLY_MIN}
    if os.path.exists(SETTINGS_JSON):
        try:
            d.update(json.load(open(SETTINGS_JSON)) or {})
        except Exception:
            pass
    return d


def save_settings(d):
    json.dump(d, open(SETTINGS_JSON, "w"), indent=2)


def early_min():
    try:
        return int(load_settings().get("early_min", DEFAULT_EARLY_MIN))
    except Exception:
        return DEFAULT_EARLY_MIN


def _min_of_day(hhmm):
    """'HH:MM' -> minutes since midnight, or None."""
    try:
        h, m = str(hhmm).split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _fmt_min(m):
    m = max(0, int(m)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def late_minutes(clock_in_str, start_str):
    """Minutes a clock-in is past its shift start (0 if on time / early)."""
    try:
        ci = datetime.strptime(str(clock_in_str).strip(), "%Y-%m-%d %H:%M")
        sm = _min_of_day(start_str)
        if sm is None:
            return None
        return max(0, (ci.hour * 60 + ci.minute) - sm)
    except Exception:
        return None


def pick_checkin_target(todays, now_min, window):
    """Choose which of today's shifts a staff member can check into now.

    Returns (row, status, open_min):
      status 'empty'     -> no shift today
      status 'done'      -> every shift already checked in
      status 'too_early' -> a pending shift exists but its window hasn't opened;
                            row = the soonest one, open_min = when it opens
      status 'ok'        -> row is checkable now
    Check-in opens at (shift start - window) and never closes (late allowed).
    """
    if todays.empty:
        return None, "empty", None
    pending = todays[todays["clock_in"].astype(str).str.strip() == ""].sort_values("start")
    if pending.empty:
        return None, "done", None
    soonest = None  # (open_min, row)
    for _, r in pending.iterrows():
        sm = _min_of_day(r["start"])
        if sm is None:
            continue
        open_m = sm - window
        if now_min >= open_m:
            return r, "ok", open_m
        if soonest is None or open_m < soonest[0]:
            soonest = (open_m, r)
    if soonest:
        return soonest[1], "too_early", soonest[0]
    return None, "empty", None


def geo_checkin_html(r, site, win_min, now_min):
    """Self-contained check-in button: captures GPS, validates geofence + time
    window, and writes the record straight to Supabase — all inside the
    component iframe. (Streamlit sandboxes components without
    allow-top-navigation, so the old redirect-the-page approach silently did
    nothing on real taps; direct REST from the iframe is the reliable path,
    the same one the live map uses.) Late check-ins always allowed."""
    url, key = _sb()
    ctx = json.dumps({
        "sb": {"url": url or "", "key": key or ""},
        "emp": str(r["employee"]), "date": str(r["date"]), "shift": str(r["shift"]),
        "branch": str(r["location"]), "branchLabel": loc_label(r["location"]),
        "start": str(r["start"]), "startMin": _min_of_day(r["start"]) or 0,
        "openMin": (_min_of_day(r["start"]) or 0) - int(win_min),
        "site": {"lat": float(site["lat"]), "lng": float(site["lng"]),
                 "radius": float(site.get("radius_m", 20))},
        "maxAcc": MAX_ACCURACY_M,
        "nowMin": int(now_min),          # server MYT minutes at render; JS adds elapsed
    })
    tmpl = """
<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif">
  <button id="gbtn" style="width:100%;min-height:58px;border:none;border-radius:14px;
    font-size:17px;font-weight:700;color:#06231f;cursor:pointer;
    background:linear-gradient(135deg,#37D7D0,#1C9C96);box-shadow:0 8px 22px rgba(55,215,208,.35)">
    📍 Check in now
  </button>
  <div id="gmsg" style="margin-top:10px;font-size:14px;color:#8AA6A0;min-height:20px;line-height:1.5"></div>
</div>
<script>
(function(){
  var C=__CTX__;
  var t0=Date.now();
  var b=document.getElementById('gbtn'), m=document.getElementById('gmsg');
  function nowMin(){ return C.nowMin + (Date.now()-t0)/60000; }
  function hhmm(mins){ mins=Math.max(0,Math.round(mins))%1440;
    return ('0'+Math.floor(mins/60)).slice(-2)+':'+('0'+(mins%60)).slice(-2); }
  function hav(a,b,c,d){ var R=6371000,p=Math.PI/180;
    var x=Math.sin((c-a)*p/2), y=Math.sin((d-b)*p/2);
    var h=x*x+Math.cos(a*p)*Math.cos(c*p)*y*y;
    return 2*R*Math.asin(Math.sqrt(h)); }
  function ok(html){ m.style.color='#67E0A3'; m.innerHTML=html; }
  function bad(html){ m.style.color='#F2A0A0'; m.innerHTML=html;
    b.disabled=false; b.style.opacity=1; }
  function hdrs(){ return {apikey:C.sb.key, Authorization:'Bearer '+C.sb.key,
                          'Content-Type':'application/json'}; }
  var qs='work_date=eq.'+C.date+'&employee=eq.'+encodeURIComponent(C.emp)+
         '&shift=eq.'+encodeURIComponent(C.shift);
  b.onclick=function(){
    if(!C.sb.url||!C.sb.key){ bad('Check-in storage is not configured — tell your admin.'); return; }
    if(!navigator.geolocation){ bad('This device does not support GPS.'); return; }
    b.disabled=true; b.style.opacity=.6;
    m.style.color='#8AA6A0'; m.textContent='Getting your location…';
    navigator.geolocation.getCurrentPosition(function(pos){
      var lat=pos.coords.latitude, lng=pos.coords.longitude,
          acc=Math.round(pos.coords.accuracy);
      if(acc>C.maxAcc){ bad('GPS signal too weak (±'+acc+' m). Move outdoors or near a window and try again.'); return; }
      var dist=Math.round(hav(lat,lng,C.site.lat,C.site.lng));
      if(dist>C.site.radius){ bad('You are about '+dist+' m from '+C.branchLabel+
        ' (must be within '+Math.round(C.site.radius)+' m). Move closer and retry.'); return; }
      var nm=nowMin();
      if(nm<C.openMin){ bad('Too early — check-in for your '+C.shift+' shift ('+C.start+
        ') opens at '+hhmm(C.openMin)+'. Come back then.'); return; }
      var late=Math.max(0,Math.round(nm-C.startMin));
      var stamp=C.date+' '+hhmm(nm);
      m.textContent='Saving your check-in…';
      // already checked in?
      fetch(C.sb.url+'/rest/v1/check_ins?select=clock_in&'+qs,{headers:hdrs()})
      .then(function(r){return r.json();})
      .then(function(rows){
        if(Array.isArray(rows)&&rows.length){
          ok('✓ You are already checked in today at <b>'+rows[0].clock_in+'</b>.');
          return null;
        }
        return fetch(C.sb.url+'/rest/v1/check_ins?on_conflict=work_date,employee,shift',{
          method:'POST',
          headers:Object.assign(hdrs(),{Prefer:'resolution=ignore-duplicates,return=minimal'}),
          body:JSON.stringify([{work_date:C.date, employee:C.emp, branch:C.branch,
            shift:C.shift, shift_start:C.start, clock_in:stamp,
            lat:+lat.toFixed(6), lng:+lng.toFixed(6), accuracy_m:acc,
            distance_m:dist, minutes_late:late}])
        }).then(function(resp){
          if(resp.status===201||resp.status===200||resp.status===204){
            ok('✓ <b>'+C.emp+'</b> checked in at <b>'+stamp+'</b> · '+C.branchLabel+' · '+
               C.shift+' shift · '+(late>0?('⏰ '+late+' min late'):'✅ on time')+
               ' · '+dist+' m from the shop.<br><span style="color:#8AA6A0">Saved. '+
               'It will show on the On-Duty map within ~20 seconds.</span>');
          } else {
            resp.text().then(function(t){ bad('Could not save — please try again. ('+resp.status+')'); });
          }
        });
      })
      .catch(function(e){ bad('Network problem while saving — check your connection and tap again.'); });
    }, function(err){
      var t={1:'Location permission denied — allow location for this site and retry.',
             2:'Position unavailable — move outdoors / near a window.',
             3:'Timed out — please try again.'};
      bad(t[err.code]||('Location error: '+err.message));
    }, {enableHighAccuracy:true, timeout:12000, maximumAge:0});
  };
})();
</script>
"""
    return tmpl.replace("__CTX__", ctx)


def geo_show_html():
    """Read-only helper for admins: shows current GPS so they can type it into
    the geofence fields. No redirect, so it never drops the admin session."""
    return """
<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif">
  <button id="lbtn" style="border:1px solid rgba(80,190,180,.4);border-radius:10px;
    padding:10px 16px;font-size:14px;font-weight:600;color:#EAF3F1;cursor:pointer;
    background:rgba(55,215,208,.14)">📍 Get my current GPS</button>
  <div id="lout" style="margin-top:9px;font-family:'JetBrains Mono',monospace;
    font-size:14px;color:#37D7D0;min-height:20px"></div>
</div>
<script>
(function(){
  var b=document.getElementById('lbtn'), o=document.getElementById('lout');
  b.onclick=function(){
    if(!navigator.geolocation){o.textContent='No GPS support.';return;}
    b.disabled=true; o.textContent='Locating…';
    navigator.geolocation.getCurrentPosition(function(p){
      b.disabled=false;
      o.innerHTML='lat '+p.coords.latitude.toFixed(6)+' &nbsp; lng '+
        p.coords.longitude.toFixed(6)+' &nbsp; (±'+Math.round(p.coords.accuracy)+
        ' m)<br><span style="color:#8AA6A0;font-family:system-ui">Type these into the branch fields, then Save.</span>';
    }, function(e){ b.disabled=false; o.textContent='Error: '+(e.message||e.code); },
    {enableHighAccuracy:true,timeout:12000,maximumAge:0});
  };
})();
</script>
"""


# ===================== SUPABASE CHECK-IN STORE =====================
# Durable check-in storage. When st.secrets has a [supabase] url+key, check-ins
# are written to / read from the Postgres `check_ins` table (survives redeploys).
# Without secrets the app falls back to the ephemeral schedule.csv (local dev).
def _sb():
    try:
        s = st.secrets.get("supabase", {})
        u, k = s.get("url"), s.get("key")
        if u and k:
            return u.rstrip("/"), k
    except Exception:
        pass
    return None, None


def db_enabled():
    return _sb()[0] is not None


def _sb_request(path, method="GET", body=None, extra_headers=None):
    """Minimal Supabase REST call using stdlib urllib (no 3rd-party deps).
    Returns (status_code, text). status_code 0 on transport error."""
    url, key = _sb()
    if not url:
        return 0, "Supabase not configured"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{url}/rest/v1/{path}", data=data, method=method)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")[:200]
    except Exception as e:
        return 0, str(e)


_SHIFT_DB2APP = {"work_date": "date", "emp_type": "type",
                 "start_time": "start", "end_time": "end"}


def db_fetch_shifts():
    """All planned shifts from Supabase as a SCHED_COLS DataFrame; None on error."""
    code, text = _sb_request("shifts?select=*&order=work_date,location,shift", "GET")
    if code != 200:
        return None
    try:
        rows = json.loads(text)
    except Exception:
        return None
    if not rows:
        return pd.DataFrame(columns=SCHED_COLS)
    df = pd.DataFrame(rows).rename(columns=_SHIFT_DB2APP)
    for c in SCHED_COLS:
        if c not in df.columns:
            df[c] = ""
    df["hours"] = pd.to_numeric(df["hours"], errors="coerce").fillna(0)
    return df[SCHED_COLS].fillna("")


def db_replace_shifts(df):
    """Replace the whole shifts table with df (delete-all then bulk insert)."""
    _sb_request("shifts?id=gt.0", "DELETE", extra_headers={"Prefer": "return=minimal"})
    if df is None or df.empty:
        return
    recs = []
    for _, r in df.iterrows():
        recs.append({
            "work_date": str(r["date"]), "day": str(r.get("day", "")),
            "location": str(r["location"]), "shift": str(r["shift"]),
            "employee": str(r["employee"]), "emp_type": str(r.get("type", "")),
            "start_time": str(r.get("start", "")), "end_time": str(r.get("end", "")),
            "hours": float(pd.to_numeric(r.get("hours", 0), errors="coerce") or 0),
            "status": str(r.get("status", "")), "note": str(r.get("note", "")),
        })
    for i in range(0, len(recs), 200):
        _sb_request("shifts", "POST", body=recs[i:i + 200],
                    extra_headers={"Prefer": "return=minimal"})


@st.cache_data(ttl=10, show_spinner=False)
def db_fetch_offdays():
    """All off-day rows from Supabase as {iso_date: [names]}; None on error."""
    code, text = _sb_request("off_days?select=work_date,employee&order=work_date", "GET")
    if code != 200:
        return None
    out = {}
    try:
        for r in json.loads(text):
            out.setdefault(str(r["work_date"]), []).append(r["employee"])
    except Exception:
        return None
    return out


def offdays_map(base_cfg):
    """The live off-day map. Supabase is the source of truth when configured
    (self-seeding once from week_config.json); else the config file."""
    if not db_enabled():
        return base_cfg.get("off_days", {})
    d = db_fetch_offdays()
    if d is None:                      # transport error — fall back this run
        return base_cfg.get("off_days", {})
    if not d and base_cfg.get("off_days"):
        recs = [{"work_date": k, "employee": n}
                for k, v in base_cfg["off_days"].items() for n in v]
        _sb_request("off_days?on_conflict=work_date,employee", "POST", body=recs,
                    extra_headers={"Prefer": "resolution=ignore-duplicates,return=minimal"})
        db_fetch_offdays.clear()
        return base_cfg.get("off_days", {})
    return d


def offday_add(diso, emp):
    """Grant emp an off day on diso. Returns (ok, err)."""
    if db_enabled():
        code, text = _sb_request("off_days?on_conflict=work_date,employee", "POST",
                                 body=[{"work_date": diso, "employee": emp}],
                                 extra_headers={"Prefer": "resolution=ignore-duplicates,return=minimal"})
        if code in (200, 201, 204):
            db_fetch_offdays.clear()
            return True, None
        return False, f"{code}: {text}"
    try:                               # local fallback: edit week_config.json
        cfg = json.load(open(CFG_JSON))
        day = cfg.setdefault("off_days", {}).setdefault(diso, [])
        if emp not in day:
            day.append(emp)
        json.dump(cfg, open(CFG_JSON, "w"), indent=2)
        return True, None
    except Exception as e:
        return False, str(e)


def offday_remove(diso, emp):
    """Remove emp's off day on diso. Returns (ok, err)."""
    if db_enabled():
        code, text = _sb_request(
            f"off_days?work_date=eq.{diso}&employee=eq.{quote(emp)}", "DELETE",
            extra_headers={"Prefer": "return=minimal"})
        if code in (200, 204):
            db_fetch_offdays.clear()
            return True, None
        return False, f"{code}: {text}"
    try:
        cfg = json.load(open(CFG_JSON))
        day = cfg.get("off_days", {}).get(diso, [])
        if emp in day:
            day.remove(emp)
            if not day:
                cfg["off_days"].pop(diso, None)
        json.dump(cfg, open(CFG_JSON, "w"), indent=2)
        return True, None
    except Exception as e:
        return False, str(e)


def db_upsert_checkin(rec):
    """Insert/update one check-in (idempotent on work_date+employee+shift).
    Returns (ok, error)."""
    if not db_enabled():
        return False, "Supabase not configured"
    code, text = _sb_request("check_ins", "POST", body=[rec],
                             extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    if code in (200, 201, 204):
        db_fetch_checkins.clear()
        return True, None
    return False, f"{code}: {text}"


@st.cache_data(ttl=10, show_spinner=False)
def db_fetch_checkins(dates_tuple):
    """Check-ins for the given ISO dates (cached ~10s). Returns list of dicts."""
    if not db_enabled() or not dates_tuple:
        return []
    q = urlencode({"select": "*", "work_date": f"in.({','.join(dates_tuple)})"})
    code, text = _sb_request(f"check_ins?{q}", "GET")
    if code == 200:
        try:
            return json.loads(text)
        except Exception:
            return []
    return []


def overlay_checkins(df):
    """Return a copy of df with clock_in/ci_* filled from the durable store.
    No-op when Supabase isn't configured (CSV values are used as-is)."""
    if df.empty or not db_enabled():
        return df
    dates = tuple(sorted(set(df["date"].astype(str))))
    rows = db_fetch_checkins(dates)
    idx = {(r["employee"], str(r["work_date"]), r["shift"]): r for r in rows}
    out = df.copy()
    for i, r in out.iterrows():
        c = idx.get((r["employee"], str(r["date"]), r["shift"]))
        if c:
            out.at[i, "clock_in"] = c.get("clock_in", "") or ""
            out.at[i, "ci_lat"] = "" if c.get("lat") is None else str(c["lat"])
            out.at[i, "ci_lng"] = "" if c.get("lng") is None else str(c["lng"])
            out.at[i, "ci_acc"] = "" if c.get("accuracy_m") is None else str(round(c["accuracy_m"]))
            out.at[i, "ci_dist"] = "" if c.get("distance_m") is None else str(round(c["distance_m"]))
            out.at[i, "ci_method"] = "gps"
        else:
            out.at[i, "clock_in"] = ""  # DB is source of truth; ignore ephemeral CSV
    return out


def _checkin_record(r, stamp, lat, lng, acc, dist, late):
    return {"work_date": r["date"], "employee": r["employee"], "branch": r["location"],
            "shift": r["shift"], "shift_start": r["start"], "clock_in": stamp,
            "lat": None if lat is None else round(lat, 6),
            "lng": None if lng is None else round(lng, 6),
            "accuracy_m": None if acc is None else round(acc),
            "distance_m": None if dist is None else round(dist),
            "minutes_late": int(late)}


def _manual_record(r, clock_in_str):
    """Check-in record for an admin manual clock-in (no GPS)."""
    lm = late_minutes(clock_in_str, r["start"]) or 0
    return {"work_date": r["date"], "employee": r["employee"], "branch": r["location"],
            "shift": r["shift"], "shift_start": r["start"], "clock_in": clock_in_str,
            "lat": None, "lng": None, "accuracy_m": None, "distance_m": None,
            "minutes_late": int(lm)}


def _fnum(x):
    try:
        return float(x)
    except Exception:
        return None


def todays_checkins():
    """Today's check-ins as a list of dicts (from Supabase, else the CSV)."""
    today = now_myt().date().isoformat()
    if db_enabled():
        return db_fetch_checkins((today,))
    df = st.session_state.schedule
    out = []
    sub = df[(df.date == today) & (df["clock_in"].astype(str).str.strip() != "")]
    for _, r in sub.iterrows():
        out.append({"employee": r["employee"], "branch": r["location"], "shift": r["shift"],
                    "clock_in": r["clock_in"], "minutes_late": late_minutes(r["clock_in"], r["start"]) or 0,
                    "lat": _fnum(r.get("ci_lat")), "lng": _fnum(r.get("ci_lng"))})
    return out


def checkin_map_html(checkins, sites, height=380):
    """Leaflet map: branch geofences + a pin per checked-in staff.
    When Supabase is configured, the map's own JS re-fetches today's check-ins
    every 20s (browser-side, publishable key + RLS) — no Streamlit reruns, so
    nothing can race the server. Live count shown as a chip on the map."""
    branches = [{"key": loc, "name": loc_label(loc), "lat": s["lat"], "lng": s["lng"],
                 "radius": int(s.get("radius_m", 20))}
                for loc, s in sites.items() if s.get("configured")]
    radii = sorted({b["radius"] for b in branches})
    radius_lbl = (f"{radii[0]}&nbsp;m" if len(radii) == 1
                  else f"{radii[0]}–{radii[-1]}&nbsp;m") if radii else "—"
    pins = []
    for c in checkins:
        br = c.get("branch")
        lat, lng = c.get("lat"), c.get("lng")
        s = sites.get(br, {})
        if lat is None or lng is None:
            if not s.get("configured"):
                continue
            lat, lng = s["lat"], s["lng"]
        pins.append({"name": c.get("employee", ""), "lat": float(lat), "lng": float(lng),
                     "branch": loc_label(br), "time": str(c.get("clock_in", ""))[11:16],
                     "late": int(c.get("minutes_late") or 0), "shift": c.get("shift", "")})
    sb_url, sb_key = _sb()
    data = json.dumps({
        "branches": branches, "pins": pins,
        "sb": {"url": sb_url or "", "key": sb_key or "",
               "today": now_myt().date().isoformat()},
    })
    tmpl = """<!DOCTYPE html><html><head>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>html,body,#m{height:100%;margin:0}body{position:relative}#m{border-radius:14px;background:#0c1a20}
.lbl{background:rgba(20,26,33,.92);border:1px solid #37D7D0;color:#EAF3F1;font:600 11px system-ui;padding:1px 6px;border-radius:6px;white-space:nowrap}
.blbl{background:rgba(12,26,32,.85);border:1px solid rgba(80,190,180,.5);color:#9fe6e0;font:700 11px system-ui;padding:2px 8px;border-radius:8px}
#lg{position:absolute;left:10px;bottom:12px;z-index:1000;background:rgba(12,26,32,.92);border:1px solid rgba(80,190,180,.4);border-radius:10px;padding:8px 11px;font:600 11.5px system-ui;color:#EAF3F1;line-height:1.75;pointer-events:none;box-shadow:0 6px 18px rgba(0,0,0,.4)}
#lg i{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:7px;vertical-align:middle}
#cnt{position:absolute;right:10px;top:10px;z-index:1000;background:rgba(12,26,32,.94);border:1px solid rgba(80,190,180,.5);border-radius:999px;padding:7px 15px;font:700 13px system-ui;color:#EAF3F1;pointer-events:none;box-shadow:0 6px 18px rgba(0,0,0,.45)}
#cnt b{color:#37D7D0;font-size:15px}</style>
</head><body><div id="m"></div>
<div id="cnt">✓ <b id="cn">0</b> checked in <span id="lv" style="color:#8AA6A0;font-weight:600"></span></div>
<div id="lg">
  <div><i style="background:#2ec878"></i>On time</div>
  <div><i style="background:#F2A03D"></i>Late</div>
  <div><i style="background:transparent;border:2px solid #37D7D0"></i>Branch · __RADIUS__</div>
</div>
<script>
var D=__DATA__;
var map=L.map('m',{scrollWheelZoom:false});
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'}).addTo(map);
var basePts=[];
var byName={};
D.branches.forEach(function(b){
  byName[b.name]=b; byName[b.key]=b;
  L.circle([b.lat,b.lng],{radius:b.radius,color:'#37D7D0',weight:1,fillColor:'#37D7D0',fillOpacity:.10}).addTo(map);
  L.marker([b.lat,b.lng],{opacity:0}).addTo(map).bindTooltip('📍 '+b.name,{permanent:true,direction:'right',offset:[6,0],className:'blbl'});
  basePts.push([b.lat,b.lng]);
});
var pinLayer=L.layerGroup().addTo(map);
var fitted=false;
var spreadGroups=[];   // same-branch pins fanned out in PIXEL space (display only)
function placeSpread(){
  if(!map._loaded) return;   // projection needs an initial view
  spreadGroups.forEach(function(g){
    var cp=map.latLngToLayerPoint(L.latLng(g.center[0], g.center[1]));
    var R=22;   // px ring — constant on-screen separation at any zoom
    g.markers.forEach(function(m,i){
      var ang=-Math.PI/2 + i*2*Math.PI/g.markers.length;
      m.setLatLng(map.layerPointToLatLng(
        L.point(cp.x+R*Math.cos(ang), cp.y+R*Math.sin(ang))));
    });
  });
}
map.on('zoomend', placeSpread);
function mkPin(p, la, ln){
  var col=p.late>0?'#F2A03D':'#2ec878';
  var m=L.circleMarker([la,ln],{radius:8,color:'#08131a',weight:2,fillColor:col,fillOpacity:.95}).addTo(pinLayer);
  m.bindTooltip(p.name,{permanent:true,direction:'top',offset:[0,-7],className:'lbl'});
  m.bindPopup('<b>'+p.name+'</b><br>'+p.branch+' · '+p.shift+'<br>🕒 '+p.time+(p.late>0?(' · ⏰ '+p.late+'m late'):' · ✅ on time'));
  return m;
}
function draw(pins){
  pinLayer.clearLayers();
  spreadGroups=[];
  var pts=basePts.slice();
  var groups={};
  pins.forEach(function(p){ (groups[p.branch]=groups[p.branch]||[]).push(p); });
  Object.keys(groups).forEach(function(gk){
    var arr=groups[gk];
    if(arr.length===1){
      var p=arr[0]; mkPin(p, p.lat, p.lng); pts.push([p.lat,p.lng]); return;
    }
    var cla=0, cln=0;
    arr.forEach(function(p){ cla+=p.lat; cln+=p.lng; });
    cla/=arr.length; cln/=arr.length;
    var ms=arr.map(function(p){ return mkPin(p, cla, cln); });
    spreadGroups.push({center:[cla,cln], markers:ms});
    pts.push([cla,cln]);
  });
  document.getElementById('cn').textContent=pins.length;
  if(!fitted){
    if(pts.length){map.fitBounds(pts,{padding:[40,40],maxZoom:16});}else{map.setView([5.95,116.08],12);}
    fitted=true;
  }
  placeSpread();   // after the view exists; zoomend re-places on any zoom
}
function rowToPin(r){
  var lat=r.lat, lng=r.lng, b=byName[r.branch]||null;
  if((lat==null||lng==null)){ if(!b) return null; lat=b.lat; lng=b.lng; }
  return {name:r.employee, lat:lat, lng:lng, branch:(b?b.name:r.branch),
          time:String(r.clock_in||'').slice(11,16), late:(r.minutes_late||0), shift:r.shift||''};
}
function refresh(){
  if(!D.sb.url||!D.sb.key) return;
  fetch(D.sb.url+'/rest/v1/check_ins?select=*&work_date=eq.'+D.sb.today,
        {headers:{apikey:D.sb.key, Authorization:'Bearer '+D.sb.key}})
    .then(function(r){return r.json();})
    .then(function(rows){
      if(!Array.isArray(rows)) return;
      draw(rows.map(rowToPin).filter(Boolean));
      var t=new Date();
      document.getElementById('lv').textContent=' · '+('0'+t.getHours()).slice(-2)+':'+('0'+t.getMinutes()).slice(-2)+':'+('0'+t.getSeconds()).slice(-2);
    }).catch(function(){});
}
draw(D.pins);
if(D.sb.url&&D.sb.key){ refresh(); setInterval(refresh,20000); }
setTimeout(function(){map.invalidateSize();},250);
</script></body></html>"""
    return tmpl.replace("__DATA__", data).replace("__RADIUS__", radius_lbl)


def do_checkin(emp, lat, lng, acc):
    """Validate a GPS check-in against today's shift + branch geofence, and
    stamp clock_in on success. Returns a result dict for the banner."""
    now = now_myt()
    today = now.date().isoformat()
    now_min = now.hour * 60 + now.minute
    df = st.session_state.schedule
    todays = df[(df.employee == emp) & (df.date == today)].sort_values("start")
    if todays.empty:
        return {"ok": False, "msg": f"No shift scheduled for {emp} today ({today}). "
                                    "Nothing to check in to — please check with your admin."}
    todays = overlay_checkins(todays)  # reflect durable (Supabase) check-ins
    target, status, open_m = pick_checkin_target(todays, now_min, early_min())
    if status == "done":
        first = todays.iloc[0]
        return {"ok": True, "already": True,
                "msg": f"{emp} is already checked in today at {first['clock_in']}. ✓"}
    if status == "too_early":
        return {"ok": False, "msg": f"Too early — check-in for your "
                                    f"{target['shift']} shift ({target['start']}) opens at "
                                    f"{_fmt_min(open_m)}. Come back then."}
    if target is None:
        return {"ok": False, "msg": "No shift available to check in to right now."}
    r = target
    target_idx = r.name
    branch = r["location"]
    site = load_sites().get(branch, {})
    if not site.get("configured"):
        return {"ok": False, "msg": f"Check-in for {loc_label(branch)} isn't enabled yet — "
                                    "ask your admin to set the branch location (Setup)."}
    if acc and acc > MAX_ACCURACY_M:
        return {"ok": False, "msg": f"GPS signal too weak (±{round(acc)} m). Move outdoors or "
                                    "near a window and try again."}
    dist = haversine_m(lat, lng, site["lat"], site["lng"])
    radius = float(site.get("radius_m", 20))
    if dist > radius:
        return {"ok": False, "msg": f"You're about {round(dist)} m from {loc_label(branch)} "
                                    f"(must be within {round(radius)} m). Move closer and retry."}
    stamp = now.strftime("%Y-%m-%d %H:%M")
    late = max(0, now_min - (_min_of_day(r["start"]) or now_min))
    if db_enabled():
        ok, err = db_upsert_checkin(_checkin_record(r, stamp, lat, lng, acc, dist, late))
        if not ok:
            return {"ok": False, "msg": "Couldn't save your check-in — please try again "
                                        f"in a moment. ({err})"}
    else:
        df.at[target_idx, "clock_in"] = stamp
        df.at[target_idx, "ci_lat"] = str(round(lat, 6))
        df.at[target_idx, "ci_lng"] = str(round(lng, 6))
        df.at[target_idx, "ci_acc"] = str(round(acc))
        df.at[target_idx, "ci_dist"] = str(round(dist))
        df.at[target_idx, "ci_method"] = "gps"
        st.session_state.schedule = df[SCHED_COLS]
        save_schedule(st.session_state.schedule)
    late_txt = f"⏰ {late} min late" if late > 0 else "✅ on time"
    return {"ok": True, "msg": f"✓ {emp} checked in at {stamp} · {loc_label(branch)} · "
                               f"{SHIFT_ICON.get(r['shift'], '')} {r['shift']} shift · {late_txt} · "
                               f"{round(dist)} m from centre (±{round(acc)} m GPS)."}


def process_checkin_qp():
    """Handle the ?att=1 redirect fired by the check-in button, then clean the URL."""
    qp = st.query_params
    if qp.get("att") != "1":
        return
    emp = qp.get("emp", "")
    try:
        lat = float(qp.get("lat"))
        lng = float(qp.get("lng"))
        acc = float(qp.get("acc", "0"))
        res = do_checkin(emp, lat, lng, acc)
    except Exception:
        res = {"ok": False, "msg": "Could not read GPS coordinates — please try again."}
    st.session_state.checkin_result = res
    st.query_params.clear()
    st.rerun()


if "schedule" not in st.session_state:
    try:
        st.session_state.schedule = load_schedule()
    except Exception as _e:                      # show real error, never "Oh no"
        st.error(f"Failed to load the schedule (build {APP_BUILD}).")
        st.exception(_e)
        st.session_state.schedule = pd.DataFrame(columns=SCHED_COLS)
if "is_admin" not in st.session_state:
    st.session_state.is_admin = False

def week_config_for(base, monday):
    dset = {(monday + timedelta(days=i)).isoformat() for i in range(7)}
    wk = dict(base)
    wk["week_start"] = monday.isoformat()
    wk["week_end"] = (monday + timedelta(days=6)).isoformat()
    wk["off_days"] = {k: v for k, v in base.get("off_days", {}).items() if k in dset}
    wk["shift_requests"] = [r for r in base.get("shift_requests", [])
                            if (not r.get("date")) or (r.get("date") in dset)]
    pa = base.get("part_availability", {})
    wk["part_availability"] = {n: {d: s for d, s in av.items() if d in dset}
                               for n, av in pa.items()}
    return wk


try:
    employees = load_employees()
    base_config = load_config()
    base_config["off_days"] = offdays_map(base_config)   # Supabase-backed leave
    if "week_start_iso" not in st.session_state:
        st.session_state.week_start_iso = base_config["week_start"]
    _ws = datetime.strptime(st.session_state.week_start_iso, "%Y-%m-%d").date()
    _ws = _ws - timedelta(days=_ws.weekday())
    config = week_config_for(base_config, _ws)
    DATES = sch.week_dates(config)
    WEEK_ISO = [d.isoformat() for d in DATES]
except Exception as _e:                          # show real error, never "Oh no"
    st.error(f"Failed to load roster/config (build {APP_BUILD}).")
    st.exception(_e)
    st.stop()


def eligible_locations(emp_name):
    row = employees[employees["name"] == emp_name]
    if row.empty:
        return list(config["locations"])
    raw = str(row.iloc[0]["locations"])
    locs = [l.strip() for l in raw.split(";") if l.strip()]
    return locs or list(config["locations"])


def emp_type(emp_name):
    row = employees[employees["name"] == emp_name]
    return str(row.iloc[0]["type"]) if not row.empty else "full"


def day_name(diso):
    return datetime.strptime(diso, "%Y-%m-%d").strftime("%A")


def parse_hhmm(s, fallback):
    try:
        h, m = str(s).split(":")
        return time(int(h), int(m))
    except Exception:
        return fallback


def calc_hours(start_t, end_t):
    s = start_t.hour + start_t.minute / 60
    e = end_t.hour + end_t.minute / 60
    if e <= s:
        e += 24
    return round(e - s, 2)


def loc_label(loc):
    return LOC_DISPLAY.get(loc, loc)


SHIFT_COLORS = {"Morning": "#F6A623", "Mid": "#3AA0FF", "Night": "#6C5CE7"}


def inject_css():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Baloo+2:wght@500;600;700;800&family=Poppins:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600;700&display=swap');
    :root{--wd-green:#1C4A42;--wd-orange:#F2A03D;--wd-teal:#37D7D0;--wd-cream:#F4EFE3;
          --ink:#0C1A20;--panel:#132630;--panel2:#16303A;--line:rgba(80,190,180,.16);
          --tx:#E9F3F1;--mut:#8AA6A0;}
    html, body, [class*="css"], .stApp{font-family:'Poppins',sans-serif;color:var(--tx);}
    .stApp{background:
      radial-gradient(900px 460px at 8% -8%, rgba(55,215,208,.16) 0%, transparent 55%),
      radial-gradient(820px 420px at 108% -6%, rgba(242,160,61,.14) 0%, transparent 55%),
      linear-gradient(160deg,#0C1A20 0%,#0A1519 60%,#0B1A1E 100%);
      background-attachment:fixed;}
    .block-container{padding-top:1.1rem;max-width:1340px;}
    h1,h2,h3,h4,h5{font-family:'Baloo 2',cursive;color:var(--tx);font-weight:700;letter-spacing:.2px;}
    .stCaption,.stCaption *{color:var(--mut) !important;}
    [data-testid="stSidebar"]{background:linear-gradient(180deg,#10262A 0%,#0B1A1D 100%);border-right:1px solid var(--line);}
    [data-testid="stSidebar"] *{color:#DCEBE8 !important;}
    .stButton>button{border-radius:12px;font-weight:600;border:1px solid var(--line);background:rgba(255,255,255,.05);color:var(--tx);transition:all .15s ease;}
    .stButton>button:hover{transform:translateY(-1px);box-shadow:0 8px 20px rgba(0,0,0,.35);border-color:var(--wd-teal);}
    .stButton>button[kind="primary"]{background:linear-gradient(135deg,#F2A03D,#E5851C);border:none;color:#231202;}
    .stDownloadButton>button{border-radius:12px;font-weight:600;}
    .stTabs [data-baseweb="tab-list"]{gap:6px;border-bottom:1px solid var(--line);}
    .stTabs [data-baseweb="tab"]{background:rgba(255,255,255,.05);border-radius:11px 11px 0 0;padding:9px 18px;font-weight:600;color:#B9CEC9;}
    .stTabs [aria-selected="true"]{background:linear-gradient(135deg,#1C4A42,#123029);}
    .stTabs [aria-selected="true"] *{color:#fff !important;}
    [data-testid="stMetric"]{background:linear-gradient(160deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:16px;padding:16px 18px;box-shadow:0 8px 24px rgba(0,0,0,.28);}
    [data-testid="stMetricValue"]{color:var(--wd-teal);font-family:'JetBrains Mono',monospace;}
    [data-testid="stMetricLabel"]{color:var(--mut) !important;}
    .stSelectbox div[data-baseweb="select"]>div,.stTextInput input,.stTimeInput input,.stNumberInput input{border-radius:10px;}
    div[data-testid="stAlert"]{border-radius:12px;}
    .wd-loc{background:linear-gradient(165deg,rgba(22,44,52,.92),rgba(15,32,38,.92));border-radius:20px;padding:16px 18px;margin:10px 0 22px;border:1px solid var(--line);box-shadow:0 12px 34px rgba(0,0,0,.34);backdrop-filter:blur(4px);}
    .wd-loc-head{display:flex;align-items:center;gap:12px;font-family:'Baloo 2';font-weight:800;font-size:22px;margin-bottom:15px;color:var(--tx);letter-spacing:.3px;}
    .wd-dot{width:14px;height:14px;border-radius:50%;box-shadow:0 0 12px 2px currentColor;}
    .wd-days{display:grid;grid-template-columns:repeat(7,1fr);gap:10px;}
    .wd-day{background:rgba(9,20,24,.55);border:1px solid rgba(120,200,190,.10);border-radius:15px;min-height:138px;overflow:hidden;}
    .wd-dhead{display:flex;align-items:baseline;gap:7px;padding:9px 11px;background:linear-gradient(135deg,rgba(55,215,208,.18),rgba(55,215,208,.05));border-bottom:1px solid rgba(120,200,190,.12);margin-bottom:9px;}
    .wd-day.wknd .wd-dhead{background:linear-gradient(135deg,rgba(242,160,61,.24),rgba(242,160,61,.07));}
    .wd-dow{font-size:12px;font-weight:800;color:var(--wd-teal);letter-spacing:1.2px;text-transform:uppercase;}
    .wd-day.wknd .wd-dow{color:#F2B96B;}
    .wd-dnum{font-family:'JetBrains Mono',monospace;font-size:23px;font-weight:700;color:#fff;line-height:1;}
    .wd-mon{font-size:11px;font-weight:700;color:var(--mut);text-transform:uppercase;letter-spacing:.5px;}
    .wd-daybody{padding:0 8px;}
    .wd-chip{display:flex;align-items:center;gap:9px;border-radius:12px;padding:8px 9px;margin-bottom:8px;background:rgba(255,255,255,.05);border:1px solid rgba(120,200,190,.10);border-left:4px solid var(--sc,#888);transition:transform .12s ease,box-shadow .12s ease,background .12s;}
    .wd-chip:hover{transform:translateY(-1px);background:rgba(255,255,255,.09);box-shadow:0 8px 20px rgba(0,0,0,.42);}
    .wd-av{flex:0 0 auto;width:34px;height:34px;border-radius:11px;color:#0c1a20;font-weight:800;font-size:13px;display:flex;align-items:center;justify-content:center;font-family:'Baloo 2';box-shadow:0 3px 9px rgba(0,0,0,.45);}
    .wd-info{min-width:0;flex:1;}
    .wd-chip .nm{font-weight:600;font-size:13px;color:#DFEDEA;line-height:1.15;}
    .wd-chip .tm{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:15px;color:#fff;margin-top:3px;letter-spacing:.4px;}
    .wd-tag{display:inline-block;font-size:9.5px;font-weight:800;padding:1px 7px;border-radius:999px;margin-left:5px;vertical-align:middle;text-transform:uppercase;letter-spacing:.4px;}
    .wd-chip.me{outline:2px solid var(--wd-orange);outline-offset:1px;background:rgba(242,160,61,.12);}
    .wd-badge{display:inline-block;font-size:9.5px;font-weight:800;padding:1px 6px;border-radius:7px;margin-left:5px;vertical-align:middle;}
    .wd-pt{background:rgba(242,160,61,.22);color:#F2C88A;}
    .wd-pin{background:rgba(226,59,46,.26);color:#F3A79F;}
    .wd-empty{color:#4E635E;font-size:22px;text-align:center;padding:14px 0 8px;}
    /* ---- Admin login gate ---- */
    .wd-gate{max-width:430px;margin:14px auto 6px;background:linear-gradient(165deg,rgba(22,44,52,.96),rgba(13,28,34,.97));
      border:1px solid rgba(80,190,180,.22);border-radius:24px;padding:30px 30px 26px;
      box-shadow:0 24px 60px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.05);position:relative;overflow:hidden;}
    .wd-gate::before{content:'';position:absolute;inset:0;pointer-events:none;
      background:radial-gradient(420px 180px at 50% -30%,rgba(55,215,208,.18),transparent 70%);}
    .wd-gate-lock{width:64px;height:64px;margin:0 auto 14px;border-radius:20px;
      background:linear-gradient(135deg,#F2A03D,#E5851C);display:flex;align-items:center;justify-content:center;
      font-size:30px;box-shadow:0 10px 26px rgba(242,160,61,.4);}
    .wd-gate-title{font-family:'Baloo 2',cursive;font-weight:800;font-size:25px;color:#EAF3F1;text-align:center;line-height:1.1;}
    .wd-gate-sub{color:#8AA6A0;font-size:13px;text-align:center;margin-top:6px;margin-bottom:6px;}
    .wd-gate-hint{margin-top:14px;background:rgba(55,215,208,.08);border:1px dashed rgba(55,215,208,.3);
      border-radius:12px;padding:9px 12px;font-size:12px;color:#B9CEC9;text-align:center;}
    .wd-gate-hint code{font-family:'JetBrains Mono',monospace;color:#37D7D0;background:rgba(0,0,0,.25);
      padding:1px 6px;border-radius:6px;}
    .wd-sb-status{display:flex;align-items:center;gap:9px;background:rgba(46,200,120,.14);
      border:1px solid rgba(46,200,120,.3);border-radius:12px;padding:10px 12px;margin-bottom:10px;}
    .wd-sb-status .dot{width:9px;height:9px;border-radius:50%;background:#4ADE80;box-shadow:0 0 10px 1px #4ADE80;}
    .wd-sb-status .txt{font-size:13px;font-weight:700;color:#CFF3DE;}
    /* ---- Admin week toolbar ---- */
    .wd-abar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
      background:linear-gradient(135deg,rgba(28,74,66,.6),rgba(18,48,41,.6));
      border:1px solid rgba(80,190,180,.2);border-radius:16px;padding:12px 16px;margin:2px 0 14px;}
    .wd-abar-ico{font-size:22px;}
    .wd-abar-txt{font-family:'Baloo 2',cursive;font-weight:800;font-size:16px;color:#EAF3F1;line-height:1.15;}
    .wd-abar-txt small{display:block;font-family:'Poppins',sans-serif;font-weight:500;font-size:11.5px;color:#8AA6A0;letter-spacing:.2px;}
    .wd-abar-pill{margin-left:auto;font-size:11px;font-weight:800;padding:4px 12px;border-radius:999px;text-transform:uppercase;letter-spacing:.5px;}
    .wd-abar-pill.now{background:rgba(46,200,120,.2);color:#67E0A3;}
    .wd-abar-pill.next{background:rgba(242,160,61,.22);color:#F2C070;}
    .wd-abar-pill.past{background:rgba(140,160,155,.18);color:#A9BEB8;}
    /* ---- Compose card ---- */
    .wd-compose{background:linear-gradient(165deg,rgba(22,44,52,.9),rgba(15,32,38,.9));
      border:1px solid rgba(80,190,180,.18);border-radius:18px;padding:6px 18px 4px;margin:4px 0 8px;
      box-shadow:0 10px 28px rgba(0,0,0,.28);}
    .wd-compose-h{display:flex;align-items:center;gap:9px;font-family:'Baloo 2',cursive;font-weight:800;
      font-size:17px;color:#EAF3F1;padding-top:12px;}
    .stDataFrame,[data-testid="stTable"] table{border-radius:12px;overflow:hidden;}
    #MainMenu, footer{visibility:hidden;}
    @media (max-width:820px){
      .block-container{padding-left:.55rem !important;padding-right:.55rem !important;padding-top:.4rem !important;max-width:100% !important;}
      h1,h2,h3{font-size:1.18rem !important;}
      .wd-loc,.sb,.db-loc{padding:13px 13px !important;border-radius:16px !important;}
      .wd-loc-head,.sb-h{font-size:18px !important;}
      /* weekly calendar -> swipeable day cards */
      .wd-days{display:flex !important;grid-template-columns:none !important;overflow-x:auto !important;gap:10px !important;padding-bottom:9px !important;scroll-snap-type:x mandatory !important;-webkit-overflow-scrolling:touch !important;}
      .wd-day{flex:0 0 80% !important;scroll-snap-align:start;}
      /* my-shifts week -> swipeable */
      .ms-week{display:flex !important;grid-template-columns:none !important;overflow-x:auto !important;gap:10px !important;padding-bottom:9px !important;scroll-snap-type:x mandatory !important;-webkit-overflow-scrolling:touch !important;}
      .ms-card{flex:0 0 62% !important;scroll-snap-align:start;}
      .ms-head{gap:12px !important;}
      .ms-kpis{width:100%;margin-left:0 !important;}
      .ms-kpi{flex:1;min-width:64px !important;}
      /* shift board -> whole matrix scrolls horizontally */
      .sb{overflow-x:auto !important;-webkit-overflow-scrolling:touch;}
      .sb-grid{min-width:660px !important;}
      /* dashboard */
      .db-kpis{grid-template-columns:repeat(2,1fr) !important;}
      .db-today{grid-template-columns:1fr !important;}
      .db-matrix{min-width:560px !important;}
      .db-sec{font-size:15px !important;}
      .stTabs [data-baseweb="tab"]{padding:8px 12px !important;font-size:13px !important;}
      /* ---- phone-first: keep the hero compact so check-in is reachable ---- */
      .hero-img{max-height:130px !important;object-fit:cover !important;}
      /* mode/layout pickers: wrap into full, thumb-sized chips */
      [role="radiogroup"]{flex-wrap:wrap !important;gap:8px !important;}
      [role="radiogroup"] > label{flex:1 1 46% !important;margin:0 !important;
        min-height:46px !important;display:flex !important;align-items:center !important;
        justify-content:center !important;text-align:center !important;
        background:rgba(255,255,255,.05) !important;border:1px solid var(--line) !important;
        border-radius:12px !important;padding:8px 10px !important;}
      [role="radiogroup"] > label[data-checked="true"],
      [role="radiogroup"] > label:has(input:checked){
        background:linear-gradient(135deg,#1C4A42,#123029) !important;
        border-color:var(--wd-teal) !important;}
      /* tap-friendly controls everywhere */
      .stButton>button,.stDownloadButton>button{min-height:46px !important;}
      [data-baseweb="select"]>div{min-height:46px !important;}
      /* tabs scroll instead of squashing */
      .stTabs [data-baseweb="tab-list"]{overflow-x:auto !important;flex-wrap:nowrap !important;
        -webkit-overflow-scrolling:touch !important;scrollbar-width:none !important;}
      .stTabs [data-baseweb="tab-list"]::-webkit-scrollbar{display:none !important;}
      /* the GPS check-in iframe button spans full width */
      iframe{width:100% !important;}
    }
    @media (max-width:480px){
      .wd-day{flex:0 0 86% !important;}
      .ms-card{flex:0 0 78% !important;}
      .db-kpi .v{font-size:26px !important;}
      h1,h2,h3{font-size:1.1rem !important;}
    }
    </style>
    """, unsafe_allow_html=True)


@st.cache_data
def _hero_uri():
    import base64
    p = "assets/hero.jpg"
    if not os.path.exists(p):
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(open(p, "rb").read()).decode()


def render_hero():
    uri = _hero_uri()
    if not uri:
        st.markdown("### 🧋 WeDrink Sabah — Shift Dashboard")
        return
    st.markdown(
        "<style>"
        ".hero-wrap{position:relative;border-radius:22px;overflow:hidden;margin-bottom:10px;"
        "box-shadow:0 18px 48px rgba(0,0,0,.5);border:1px solid rgba(80,190,180,.22);"
        "background:#274A4A;}"
        ".hero-img{width:100%;display:block;height:auto;}"
        ".hero-wrap::after{content:'';position:absolute;inset:0;pointer-events:none;"
        "box-shadow:inset 0 -34px 46px -22px rgba(12,26,32,.55),inset 0 0 0 1px rgba(255,255,255,.04);"
        "border-radius:22px;}"
        ".hero-tag{position:absolute;left:16px;bottom:12px;z-index:2;font-family:'Baloo 2',cursive;"
        "font-weight:800;font-size:13px;letter-spacing:.5px;color:#EAF3F1;background:rgba(12,26,32,.55);"
        "backdrop-filter:blur(4px);padding:5px 12px;border-radius:999px;border:1px solid rgba(80,190,180,.3);}"
        "@media (max-width:640px){.hero-tag{display:none;}}"
        "</style>"
        '<div class="hero-wrap"><img class="hero-img" src="' + uri + '" alt="WeDrink Sabah"/>'
        '<span class="hero-tag">🧋 Shift Dashboard · Kota Kinabalu</span></div>',
        unsafe_allow_html=True)


def grid_html(sched, loc, who=None):
    color = LOC_COLORS.get(loc, "#37D7D0")
    days_html = ""
    for d in DATES:
        diso = d.isoformat()
        wknd = " wknd" if d.weekday() >= 5 else ""
        cell = sched[(sched.location == loc) & (sched.date == diso)].sort_values("start")
        chips = ""
        for _, r in cell.iterrows():
            col = SHIFT_COLORS.get(r["shift"], "#8AA6A0")
            me = " me" if who and who != "— show all —" and r["employee"] == who else ""
            pt = '<span class="wd-badge wd-pt">PT</span>' if r["type"] == "part" else ""
            pin = '<span class="wd-badge wd-pin">★</span>' if r["status"] == "pinned" else ""
            ci = str(r.get("clock_in", "")).strip()
            ck = (f'<span class="wd-badge" style="background:rgba(46,200,120,.22);color:#67E0A3">'
                  f'✓ {ci[11:16] or ci}</span>') if ci else ""
            keyb = ('<span class="wd-badge" title="Key holder" '
                    'style="background:rgba(242,160,61,.22);color:#F2C070">🔑</span>'
                    if "🔑" in str(r.get("note", "")) else "")
            nm = str(r["employee"]).strip()
            initials = (nm[0] + (nm[1] if len(nm) > 1 else "")).upper()
            chips += (f'<div class="wd-chip{me}" style="--sc:{col}">'
                      f'<div class="wd-av" style="background:{col}">{initials}</div>'
                      f'<div class="wd-info">'
                      f'<div class="nm">{nm}'
                      f'<span class="wd-tag" style="background:{col}26;color:{col}">{r["shift"]}</span>'
                      f'{pt}{pin}{keyb}{ck}</div>'
                      f'<div class="tm">{r["start"]}&ndash;{r["end"]}</div>'
                      f'</div></div>')
        body = chips if chips else '<div class="wd-empty">&mdash;</div>'
        days_html += (f'<div class="wd-day{wknd}"><div class="wd-dhead">'
                      f'<span class="wd-dow">{d.strftime("%a")}</span>'
                      f'<span class="wd-dnum">{d.strftime("%d")}</span>'
                      f'<span class="wd-mon">{d.strftime("%b")}</span></div>'
                      f'<div class="wd-daybody">{body}</div></div>')
    return (f'<div class="wd-loc"><div class="wd-loc-head">'
            f'<span class="wd-dot" style="color:{color};background:{color}"></span>'
            f'{LOC_EMOJI.get(loc, "📍")} {loc_label(loc)}</div>'
            f'<div class="wd-days">{days_html}</div></div>')


inject_css()
render_hero()
st.caption(
    f"Week {config['week_start']} → {config['week_end']}  •  {config['region']}  •  "
    f"Time zone: Asia/Kuching (UTC+8)  •  Now: {now_myt().strftime('%a %d %b %Y, %H:%M')}"
)

# Handle a GPS check-in redirect (?att=1…); runs here so all helpers are defined.
try:
    process_checkin_qp()
except Exception as _e:
    st.error(f"Check-in processing failed (build {APP_BUILD}).")
    st.exception(_e)

# GPS check-in result (survives the check-in page reload via session_state).
_ci_res = st.session_state.pop("checkin_result", None)
if _ci_res:
    if _ci_res.get("ok") and not _ci_res.get("already"):
        st.success(_ci_res["msg"])
    elif _ci_res.get("already"):
        st.info(_ci_res["msg"])
    else:
        st.error(_ci_res["msg"])

with st.sidebar:
    st.markdown("### 👤 View")
    view_mode = st.radio("Choose view", ["👀 Overall (staff)", "🔐 Admin"],
                         label_visibility="collapsed", key="view_mode")
    st.divider()
    if view_mode == "🔐 Admin":
        if st.session_state.is_admin:
            a = load_admin()
            st.markdown(
                "<div class='wd-sb-status'><span class='dot'></span>"
                f"<span class='txt'>Signed in · {a['user']}</span></div>",
                unsafe_allow_html=True)
            if st.button("🚪 Log out", width="stretch"):
                st.session_state.is_admin = False
                st.rerun()
        else:
            st.info("🔐 Admin area — sign in on the main panel to edit the schedule.")

IS_ADMIN = st.session_state.is_admin


def render_week_nav(context="overall"):
    ws = datetime.strptime(st.session_state.week_start_iso, "%Y-%m-%d").date()
    ws = ws - timedelta(days=ws.weekday())
    we = ws + timedelta(days=6)
    week_iso = [(ws + timedelta(days=i)).isoformat() for i in range(7)]
    has = not st.session_state.schedule[st.session_state.schedule.date.isin(week_iso)].empty
    today = now_myt().date()
    is_now = ws <= today <= we
    c1, c2, c3, c4 = st.columns([1, 3, 1, 2])
    if c1.button("◀", width="stretch", key=f"wkprev_{context}"):
        st.session_state.week_start_iso = (ws - timedelta(days=7)).isoformat()
        st.rerun()
    badge = ("🟢 has shifts" if has else "⚪ empty") + (" · this week" if is_now else "")
    c2.markdown(
        "<div style='text-align:center;padding:5px 0;'>"
        "<div style='font-family:Baloo 2,cursive;font-weight:800;font-size:18px;color:#EAF3F1;'>"
        f"📅 {ws.strftime('%d %b')} – {we.strftime('%d %b %Y')}</div>"
        f"<div style='font-size:11.5px;color:#8AA6A0;'>{badge}</div></div>",
        unsafe_allow_html=True)
    if c3.button("▶", width="stretch", key=f"wknext_{context}"):
        st.session_state.week_start_iso = (ws + timedelta(days=7)).isoformat()
        st.rerun()
    jump = c4.date_input("Jump to week", value=ws, label_visibility="collapsed",
                         key=f"wkjump_{context}_{ws.isoformat()}")
    jm = jump - timedelta(days=jump.weekday())
    if jm.isoformat() != ws.isoformat():
        st.session_state.week_start_iso = jm.isoformat()
        st.rerun()


def render_checkin_ui():
    now = now_myt()
    today = now.date()
    now_min = now.hour * 60 + now.minute
    win = early_min()
    st.markdown("#### 🟢 Start-of-shift GPS Check-In")
    st.caption(f"Pick your name, then tap **Check in now** at your branch.  Check-in opens "
               f"**{win} min** before your shift.  Today: {today.strftime('%a %d %b %Y')} · "
               f"{now.strftime('%H:%M')} (Sabah time)")
    who = st.selectbox("Your name", ["— select your name —"] + list(employees["name"]),
                       key="ci_who")
    if who == "— select your name —":
        st.info("Select your name above to check in.")
        return

    df = st.session_state.schedule
    todays = df[(df.employee == who) & (df.date == today.isoformat())].sort_values("start")
    if todays.empty:
        st.warning(f"No shift scheduled for **{who}** today. "
                   "If that's wrong, please ask your admin to add it.")
        return
    todays = overlay_checkins(todays)  # reflect durable (Supabase) check-ins

    # Show today's shift(s) and their check-in status.
    for _, r in todays.iterrows():
        done = str(r["clock_in"]).strip()
        col = SHIFT_COLORS.get(r["shift"], "#37D7D0")
        if done:
            lm = late_minutes(done, r["start"])
            tag = (f" · ⏰ {lm} min late" if lm else " · ✅ on time")
            status = f"<span style='color:#67E0A3;font-weight:700'>✓ checked in {done}{tag}</span>"
        else:
            opens = _fmt_min((_min_of_day(r["start"]) or 0) - win)
            status = (f"<span style='color:#F2C070;font-weight:700'>not checked in</span>"
                      f"<span style='color:#8AA6A0'> · opens {opens}</span>")
        st.markdown(
            f"<div style='background:rgba(9,20,24,.55);border:1px solid rgba(120,200,190,.14);"
            f"border-left:4px solid {col};border-radius:12px;padding:11px 14px;margin:6px 0'>"
            f"<b>{LOC_EMOJI.get(r['location'],'📍')} {loc_label(r['location'])}</b> · "
            f"{SHIFT_ICON.get(r['shift'],'')} {r['shift']} · {r['start']}–{r['end']} &nbsp; {status}"
            f"</div>", unsafe_allow_html=True)

    target, status, open_m = pick_checkin_target(todays, now_min, win)
    if status == "done":
        st.success("You're all checked in for today — have a great shift! 🧋")
        return
    if status == "too_early":
        st.info(f"⏳ Check-in for your **{target['shift']}** shift ({target['start']}) opens at "
                f"**{_fmt_min(open_m)}** — {win} min before start. Come back then.")
        return
    if target is None:
        st.info("No shift available to check in to right now.")
        return

    r = target
    branch = r["location"]
    site = load_sites().get(branch, {})
    radius = int(site.get("radius_m", 20))
    if not site.get("configured"):
        st.error(f"Check-in for **{loc_label(branch)}** isn't set up yet. "
                 "Ask your admin to capture the branch location under **Admin ▸ Setup**.")
        return

    late = max(0, now_min - (_min_of_day(r["start"]) or now_min))
    late_note = (f"  ⏰ You're **{late} min** past the {r['start']} start — this will record as a "
                 "late check-in." if late > 0 else "")
    st.markdown(f"**Checking in to {loc_label(branch)} — {SHIFT_ICON.get(r['shift'],'')} "
                f"{r['shift']} shift.** You must be within **{radius} m** of the shop.{late_note}")
    components.html(geo_checkin_html(r, site, win, now_min), height=160)
    st.caption("Your location is captured only when you tap the button, and only used to "
               "confirm you're at the branch. Works on the deployed https:// link "
               "(phone browser will ask for location permission).")


def render_checkin_map():
    """On-duty check-in map. The refresh happens INSIDE the map iframe (its JS
    polls Supabase every 20s with the publishable key) — no st.fragment, no
    server reruns, so nothing can race Streamlit and crash the app."""
    st.markdown("##### 🗺️ Checked in today &nbsp;·&nbsp; 🔄 live")
    components.html(checkin_map_html(todays_checkins(), load_sites()), height=410)
    st.caption("Count and pins update automatically every 20 seconds · "
               "tap a pin for name & time.")


def offday_board_html(off_map, dates):
    """Overall 'who's on leave this week' table — one row per day."""
    css = """<style>
    .lv{width:100%;border-collapse:collapse;margin:4px 0 8px;
      background:linear-gradient(165deg,rgba(22,44,52,.92),rgba(15,32,38,.92));
      border:1px solid rgba(80,190,180,.18);border-radius:16px;overflow:hidden;
      box-shadow:0 10px 28px rgba(0,0,0,.28);}
    .lv td{padding:11px 14px;border-bottom:1px solid rgba(120,200,190,.1);vertical-align:middle;}
    .lv tr:last-child td{border-bottom:none;}
    .lv tr.wknd{background:rgba(242,160,61,.06);}
    .lv-day{white-space:nowrap;color:#CFE3DE;font-weight:600;font-size:13px;width:132px;}
    .lv-day .dw{color:#37D7D0;font-weight:800;}
    .lv-day.wk .dw{color:#F2B96B;}
    .lv-chip{display:inline-block;background:rgba(242,160,61,.2);color:#F2C070;
      border:1px solid rgba(242,160,61,.32);border-radius:999px;padding:3px 12px;
      margin:2px 5px 2px 0;font-size:12.5px;font-weight:700;}
    .lv-none{color:#5B726C;font-size:12.5px;font-style:italic;}
    </style>"""
    body = ""
    total = 0
    for d in dates:
        names = off_map.get(d.isoformat(), [])
        total += len(names)
        wk = " wknd" if d.weekday() >= 5 else ""
        wkc = " wk" if d.weekday() >= 5 else ""
        chips = ("".join(f"<span class='lv-chip'>🛌 {n}</span>" for n in names)
                 if names else "<span class='lv-none'>— full team working —</span>")
        body += (f"<tr class='{wk}'><td class='lv-day{wkc}'>"
                 f"<span class='dw'>{d.strftime('%a')}</span> {d.strftime('%d %b')}</td>"
                 f"<td>{chips}</td></tr>")
    return css + f"<table class='lv'>{body}</table>", total


def attendance_html(sweek, dates):
    """Week attendance matrix: staff x day — on time / late(+min) / missed /
    upcoming, built from the schedule and the durable check-in records."""
    today = now_myt().date()
    cks = db_fetch_checkins(tuple(d.isoformat() for d in dates)) if db_enabled() else []
    idx = {(c["employee"], str(c["work_date"]), c["shift"]): c for c in cks}
    staff = sorted(sweek["employee"].unique(),
                   key=lambda n: (emp_type(n) != "full", n))
    css = """<style>
    .at{background:linear-gradient(165deg,rgba(22,44,52,.92),rgba(15,32,38,.92));
      border:1px solid rgba(80,190,180,.18);border-radius:18px;padding:14px 16px;margin:6px 0 14px;
      box-shadow:0 10px 28px rgba(0,0,0,.3);overflow-x:auto;-webkit-overflow-scrolling:touch;}
    .at-grid{display:grid;grid-template-columns:112px repeat(7,minmax(86px,1fr));gap:6px;min-width:740px;}
    .at-dh{text-align:center;padding:7px 2px;border-radius:10px;background:rgba(55,215,208,.10);
      font-size:11px;font-weight:800;color:#37D7D0;text-transform:uppercase;}
    .at-dh.wknd{background:rgba(242,160,61,.14);color:#F2B96B;}
    .at-dh.today{outline:2px solid rgba(55,215,208,.55);}
    .at-dh b{display:block;font-family:'JetBrains Mono',monospace;font-size:15px;color:#fff;}
    .at-nm{display:flex;align-items:center;gap:7px;font-weight:700;font-size:12.5px;color:#EAF3F1;padding:0 4px;}
    .at-av{flex:0 0 auto;width:26px;height:26px;border-radius:8px;background:linear-gradient(135deg,#37D7D0,#1C9C96);
      color:#06231f;font-weight:800;font-size:11px;display:flex;align-items:center;justify-content:center;}
    .at-c{border-radius:11px;min-height:44px;display:flex;flex-direction:column;align-items:center;
      justify-content:center;gap:1px;font-size:11px;font-weight:700;padding:5px 2px;text-align:center;}
    .at-c small{font-family:'JetBrains Mono',monospace;font-size:11.5px;font-weight:700;}
    .at-ok{background:rgba(46,200,120,.16);border:1px solid rgba(46,200,120,.3);color:#67E0A3;}
    .at-late{background:rgba(242,160,61,.15);border:1px solid rgba(242,160,61,.32);color:#F2C070;}
    .at-miss{background:rgba(226,91,77,.13);border:1px solid rgba(226,91,77,.3);color:#F0938A;}
    .at-wait{background:rgba(255,255,255,.03);border:1px dashed rgba(120,200,190,.18);color:#5F776F;}
    .at-rest{background:transparent;border:none;color:#37504A;font-size:16px;}
    .at-kpis{display:flex;gap:10px;flex-wrap:wrap;margin:2px 0 12px;}
    .at-kpi{background:rgba(255,255,255,.05);border:1px solid rgba(80,190,180,.16);border-radius:13px;
      padding:8px 16px;text-align:center;min-width:86px;}
    .at-kpi .v{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:20px;line-height:1;}
    .at-kpi .l{font-size:10px;color:#8AA6A0;text-transform:uppercase;letter-spacing:.5px;margin-top:4px;}
    </style>"""
    n_ok = n_late = n_miss = n_wait = 0
    hdr = "<div class='at-nm'></div>"
    for d in dates:
        cls = " wknd" if d.weekday() >= 5 else ""
        cls += " today" if d == today else ""
        hdr += f"<div class='at-dh{cls}'>{d.strftime('%a')}<b>{d.strftime('%d')}</b></div>"
    rows = ""
    for nm in staff:
        initials = (nm[0] + (nm[1] if len(nm) > 1 else "")).upper()
        rows += (f"<div class='at-nm'><span class='at-av'>{initials}</span>{nm}"
                 f"{' ⏳' if emp_type(nm) == 'part' else ''}</div>")
        mine = sweek[sweek.employee == nm]
        for d in dates:
            diso = d.isoformat()
            day_shifts = mine[mine.date == diso].sort_values("start")
            if day_shifts.empty:
                rows += "<div class='at-c at-rest'>·</div>"
                continue
            cells = []
            for _, r in day_shifts.iterrows():
                c = idx.get((nm, diso, r["shift"]))
                if c:
                    late = int(c.get("minutes_late") or 0)
                    t = str(c.get("clock_in", ""))[11:16]
                    if late > 0:
                        cells.append(("late", f"⏰<small>{t}</small>+{late}m"))
                    else:
                        cells.append(("ok", f"✓<small>{t}</small>on time"))
                elif d < today:
                    cells.append(("miss", f"✗<small>{r['start']}</small>no check-in"))
                else:
                    cells.append(("wait", f"·<small>{r['start']}</small>upcoming"))
            for k, _ in cells:
                n_ok += k == "ok"; n_late += k == "late"
                n_miss += k == "miss"; n_wait += k == "wait"
            kind = cells[0][0]
            inner = "<br>".join(x[1] for x in cells) if len(cells) > 1 else cells[0][1]
            rows += f"<div class='at-c at-{kind}'>{inner}</div>"
    done = n_ok + n_late
    kpis = (f"<div class='at-kpis'>"
            f"<div class='at-kpi'><div class='v' style='color:#37D7D0'>{done}</div><div class='l'>Checked in</div></div>"
            f"<div class='at-kpi'><div class='v' style='color:#67E0A3'>{n_ok}</div><div class='l'>On time</div></div>"
            f"<div class='at-kpi'><div class='v' style='color:#F2C070'>{n_late}</div><div class='l'>Late</div></div>"
            f"<div class='at-kpi'><div class='v' style='color:#F0938A'>{n_miss}</div><div class='l'>No check-in</div></div>"
            f"<div class='at-kpi'><div class='v' style='color:#8AA6A0'>{n_wait}</div><div class='l'>Upcoming</div></div>"
            f"</div>")
    return css + kpis + f"<div class='at'><div class='at-grid'>{hdr}{rows}</div></div>"


RACE_VEHICLES = ["🏎️", "🚗", "🚙", "🛻", "🚕", "🏍️", "🛵", "🚌", "🚐", "🚜",
                 "🛺", "🚓", "🚲", "🛴"]


def race_html(sweek, dates):
    """🏁 Punctuality Grand Prix — friendly weekly race built from check-ins.
    Every on-time check-in pushes a racer forward; late = time in the pits."""
    cks = db_fetch_checkins(tuple(d.isoformat() for d in dates)) if db_enabled() else []
    stats = {}
    for c in cks:
        s = stats.setdefault(c["employee"], {"ok": 0, "late": 0, "mins": 0})
        if int(c.get("minutes_late") or 0) > 0:
            s["late"] += 1
            s["mins"] += int(c["minutes_late"])
        else:
            s["ok"] += 1
    css = """<style>
    .gp{background:linear-gradient(165deg,rgba(22,44,52,.94),rgba(15,32,38,.94));
      border:1px solid rgba(80,190,180,.2);border-radius:18px;padding:16px 18px;margin:14px 0 6px;
      box-shadow:0 12px 30px rgba(0,0,0,.32);}
    .gp-h{font-family:'Baloo 2',cursive;font-weight:800;font-size:19px;color:#EAF3F1;margin-bottom:2px;}
    .gp-sub{color:#8AA6A0;font-size:12px;margin-bottom:14px;}
    .gp-pod{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;}
    .gp-card{flex:1;min-width:150px;border-radius:14px;padding:11px 14px;border:1px solid;}
    .gp-card .t{font-size:10.5px;font-weight:800;text-transform:uppercase;letter-spacing:.7px;}
    .gp-card .n{font-family:'Baloo 2',cursive;font-weight:800;font-size:19px;color:#fff;margin-top:3px;}
    .gp-card .d{font-size:11.5px;margin-top:2px;}
    .gp-gold{background:rgba(255,204,84,.12);border-color:rgba(255,204,84,.45);}
    .gp-gold .t,.gp-gold .d{color:#FFD778;}
    .gp-silv{background:rgba(200,214,222,.10);border-color:rgba(200,214,222,.38);}
    .gp-silv .t,.gp-silv .d{color:#CFDDE4;}
    .gp-brz{background:rgba(222,148,90,.10);border-color:rgba(222,148,90,.4);}
    .gp-brz .t,.gp-brz .d{color:#E8AE85;}
    .gp-pit{background:rgba(242,160,61,.09);border-color:rgba(242,160,61,.3);}
    .gp-pit .t,.gp-pit .d{color:#F2C070;}
    .gp-row{display:flex;align-items:center;gap:9px;margin:7px 0;}
    .gp-nm{flex:0 0 74px;font-weight:700;font-size:12px;color:#DFEDEA;text-align:right;}
    .gp-track{flex:1;position:relative;height:30px;background:
      repeating-linear-gradient(90deg,rgba(255,255,255,.045) 0 26px,rgba(255,255,255,.015) 26px 52px);
      border:1px solid rgba(120,200,190,.12);border-radius:999px;overflow:hidden;}
    .gp-track::after{content:'🏁';position:absolute;right:7px;top:4px;font-size:14px;opacity:.75;}
    .gp-run{position:absolute;left:0;top:0;height:100%;display:flex;align-items:center;
      justify-content:flex-end;font-size:19px;
      background:linear-gradient(90deg,rgba(55,215,208,.05),rgba(55,215,208,.16));
      border-radius:999px;transition:width .6s ease;}
    .gp-sc{flex:0 0 auto;font-family:'JetBrains Mono',monospace;font-size:11.5px;font-weight:700;
      color:#8AA6A0;min-width:88px;}
    .gp-sc b{color:#67E0A3;} .gp-sc i{color:#F2C070;font-style:normal;}
    .gp-grid{color:#5F776F;font-size:12px;margin-top:10px;}
    </style>"""
    if not stats:
        return css + ("<div class='gp'><div class='gp-h'>🏁 Punctuality Grand Prix</div>"
                      "<div class='gp-sub'>The race starts with this week's first "
                      "check-in — lights out and away we go! 🚦</div></div>")
    rank = sorted(stats.items(), key=lambda kv: (-kv[1]["ok"], kv[1]["mins"], kv[0]))
    podium = ""
    medals = [("gp-gold", "🥇 Pole Position"), ("gp-silv", "🥈 Front Row"),
              ("gp-brz", "🥉 Podium")]
    for (cls, title), (nm, s) in zip(medals, [r for r in rank if r[1]["ok"] > 0][:3]):
        d = f"{s['ok']} on-time" + (f" · {s['mins']}m in the pits" if s["mins"] else " · clean laps")
        podium += (f"<div class='gp-card {cls}'><div class='t'>{title}</div>"
                   f"<div class='n'>{nm}</div><div class='d'>{d}</div></div>")
    pits = sorted([r for r in rank if r[1]["mins"] > 0], key=lambda kv: -kv[1]["mins"])[:1]
    for nm, s in pits:
        podium += (f"<div class='gp-card gp-pit'><div class='t'>⛽ Longest Pit Stop</div>"
                   f"<div class='n'>{nm}</div><div class='d'>{s['mins']} min in the pits — "
                   f"fresh tyres, comeback loading 🔧</div></div>")
    vehicles = {nm: RACE_VEHICLES[i % len(RACE_VEHICLES)]
                for i, nm in enumerate(sorted(sweek["employee"].unique()))}
    max_ok = max(s["ok"] for s in stats.values()) or 1
    track = ""
    for nm, s in rank:
        pct = max(10, round(100 * s["ok"] / max_ok)) if s["ok"] else 8
        sc = f"<b>{s['ok']}✓</b>"
        if s["late"]:
            sc += f" <i>{s['late']}⏰+{s['mins']}m</i>"
        track += (f"<div class='gp-row'><div class='gp-nm'>{nm}</div>"
                  f"<div class='gp-track'><div class='gp-run' style='width:{pct}%'>"
                  f"{vehicles.get(nm, '🚗')}</div></div>"
                  f"<div class='gp-sc'>{sc}</div></div>")
    on_grid = sorted(set(sweek["employee"].unique()) - set(stats))
    grid = (f"<div class='gp-grid'>🚦 Still on the starting grid: "
            f"{', '.join(f'{vehicles.get(n)} {n}' for n in on_grid)}</div>" if on_grid else "")
    return css + (f"<div class='gp'><div class='gp-h'>🏁 Punctuality Grand Prix</div>"
                  f"<div class='gp-sub'>Every on-time check-in pushes your racer towards the "
                  f"flag — ties go to the fewest minutes in the pits. New week, new race!</div>"
                  f"<div class='gp-pod'>{podium}</div>{track}{grid}</div>")


def render_overall():
    sched = st.session_state.schedule
    st.subheader("🧋 WeDrink Sabah")
    mode = st.radio("view", ["🟢 Check In", "📊 On Duty",
                             "🗓️ Schedule", "✅ Attendance", "🙋 My Shifts"],
                    horizontal=True, label_visibility="collapsed")
    if mode.startswith("🟢"):
        # Check-in is a today-only action — no week navigation needed.
        render_checkin_ui()
        return
    render_week_nav("overall")
    sweek = sched[sched.date.isin(WEEK_ISO)]
    if mode.startswith("📊"):
        render_checkin_map()
        st.markdown(dashboard_html(sweek), unsafe_allow_html=True)
    elif mode.startswith("🗓️"):
        # On-leave overview for the week (visible to everyone).
        board, n_off = offday_board_html(config.get("off_days", {}), DATES)
        with st.expander(f"🛌 On leave this week — {n_off} off-day request(s)",
                         expanded=bool(n_off)):
            st.markdown(board, unsafe_allow_html=True)
            noff = config.get("no_off_members", [])
            if noff:
                st.caption("💪 Working all 7 days (requested no off): " + ", ".join(noff))
        if sweek.empty:
            st.info("No shifts for this week yet. Ask your Admin to create the schedule.")
        else:
            sweek = overlay_checkins(sweek)  # show ✓ on staff who've checked in
            style = st.radio("layout", ["🗓️ Calendar cards", "🔲 Shift board"],
                             horizontal=True, label_visibility="collapsed")
            for loc in config["locations"]:
                if style.startswith("🗓️"):
                    st.markdown(grid_html(sweek, loc), unsafe_allow_html=True)
                else:
                    st.markdown(shift_board_html(sweek, loc), unsafe_allow_html=True)
            st.caption("✓ = checked in · 🔑 = key holder · ⏳ = part-timer · read-only view.")
    elif mode.startswith("✅"):
        st.markdown("#### ✅ Attendance — check-in history for this week")
        if sweek.empty:
            st.info("No shifts scheduled for this week, so there is no attendance to show.")
        else:
            # Race first — the fun, motivating headline; detailed matrix below.
            st.markdown(race_html(sweek, DATES), unsafe_allow_html=True)
            st.markdown("##### 📋 Day-by-day detail")
            st.markdown(attendance_html(sweek, DATES), unsafe_allow_html=True)
            st.caption("✓ green = on time · ⏰ amber = late (+minutes) · ✗ red = no check-in "
                       "(past days) · dashed = shift not started yet · ⏳ = part-timer. "
                       "Times are actual GPS check-ins recorded at the shop.")
    else:
        who = st.selectbox("🔎 Choose your name to see your week",
                           ["— select —"] + list(employees["name"]))
        if who != "— select —":
            mine = sweek[sweek.employee == who].sort_values(["date", "start"])
            if mine.empty:
                st.warning(f"No shifts scheduled for {who} in this week.")
            else:
                st.markdown(my_shifts_html(mine, who), unsafe_allow_html=True)


def my_shifts_html(mine, who):
    nm = str(who).strip()
    initials = (nm[0] + (nm[1] if len(nm) > 1 else "")).upper()
    et = emp_type(who)
    hrs_series = pd.to_numeric(mine["hours"], errors="coerce").fillna(0)
    total = round(hrs_series.sum(), 1)
    days_worked = mine["date"].nunique()
    rest = len(DATES) - days_worked
    locs = sorted(set(loc_label(x) for x in mine["location"]))
    css = """<style>
    .ms-wrap{background:linear-gradient(165deg,rgba(22,44,52,.94),rgba(15,32,38,.94));border:1px solid rgba(80,190,180,.18);border-radius:20px;padding:18px 20px;margin:4px 0 12px;box-shadow:0 14px 36px rgba(0,0,0,.36);}
    .ms-head{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:18px;}
    .ms-av{width:60px;height:60px;border-radius:17px;background:linear-gradient(135deg,#37D7D0,#1C9C96);color:#06231f;font-family:'Baloo 2',cursive;font-weight:800;font-size:24px;display:flex;align-items:center;justify-content:center;box-shadow:0 6px 18px rgba(55,215,208,.35);}
    .ms-name{font-family:'Baloo 2',cursive;font-weight:800;font-size:25px;color:#EAF3F1;line-height:1;}
    .ms-sub{color:#8AA6A0;font-size:13px;margin-top:5px;}
    .ms-sub b{color:#B9CEC9;font-weight:600;}
    .ms-kpis{display:flex;gap:10px;margin-left:auto;flex-wrap:wrap;}
    .ms-kpi{background:rgba(255,255,255,.05);border:1px solid rgba(80,190,180,.16);border-radius:14px;padding:9px 15px;text-align:center;min-width:76px;}
    .ms-kpi .v{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:21px;color:#37D7D0;line-height:1;}
    .ms-kpi.o .v{color:#F2A03D;}
    .ms-kpi .l{font-size:10px;color:#8AA6A0;text-transform:uppercase;letter-spacing:.6px;margin-top:5px;}
    .ms-week{display:grid;grid-template-columns:repeat(7,1fr);gap:10px;}
    .ms-card{background:rgba(9,20,24,.6);border:1px solid rgba(120,200,190,.12);border-radius:15px;overflow:hidden;border-top:3px solid var(--sc,#37D7D0);}
    .ms-card.rest{border-top-color:#31423D;opacity:.72;}
    .ms-dh{display:flex;align-items:baseline;gap:6px;padding:8px 11px;background:rgba(55,215,208,.10);}
    .ms-dh.wknd{background:rgba(242,160,61,.16);}
    .ms-dow{font-size:11px;font-weight:800;color:#37D7D0;letter-spacing:1px;text-transform:uppercase;}
    .ms-dh.wknd .ms-dow{color:#F2B96B;}
    .ms-dn{font-family:'JetBrains Mono',monospace;font-size:20px;font-weight:700;color:#fff;}
    .ms-mon{font-size:10px;font-weight:700;color:#8AA6A0;text-transform:uppercase;}
    .ms-body{padding:10px 11px;}
    .ms-loc{font-size:11px;font-weight:700;color:#B9CEC9;margin-bottom:6px;}
    .ms-tag{display:inline-block;font-size:9.5px;font-weight:800;padding:1px 8px;border-radius:999px;text-transform:uppercase;letter-spacing:.4px;margin-bottom:8px;}
    .ms-time{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:16px;color:#fff;letter-spacing:.4px;}
    .ms-hrs{font-size:11px;color:#8AA6A0;margin-top:4px;}
    .ms-off{padding:20px 11px 22px;text-align:center;}
    .ms-off .o{font-size:13px;font-weight:800;letter-spacing:1.5px;color:#5F776F;}
    .ms-off .e{font-size:20px;}
    </style>"""
    cards = ""
    for d in DATES:
        diso = d.isoformat()
        wknd = " wknd" if d.weekday() >= 5 else ""
        day_rows = mine[mine.date == diso].sort_values("start")
        head = (f'<div class="ms-dh{wknd}"><span class="ms-dow">{d.strftime("%a")}</span>'
                f'<span class="ms-dn">{d.strftime("%d")}</span>'
                f'<span class="ms-mon">{d.strftime("%b")}</span></div>')
        if day_rows.empty:
            cards += (f'<div class="ms-card rest">{head}'
                      f'<div class="ms-off"><div class="e">🌴</div>'
                      f'<div class="o">REST DAY</div></div></div>')
        else:
            body = ""
            top_col = SHIFT_COLORS.get(day_rows.iloc[0]["shift"], "#37D7D0")
            for _, r in day_rows.iterrows():
                col = SHIFT_COLORS.get(r["shift"], "#37D7D0")
                body += (f'<div class="ms-loc">📍 {loc_label(r["location"])}</div>'
                         f'<span class="ms-tag" style="background:{col}26;color:{col}">{r["shift"]}</span>'
                         f'<div class="ms-time">{r["start"]}&ndash;{r["end"]}</div>'
                         f'<div class="ms-hrs">{r["hours"]} h'
                         f'{" · fixed ★" if r["status"]=="pinned" else ""}'
                         f'{" · PT" if r["type"]=="part" else ""}</div>')
            cards += (f'<div class="ms-card" style="--sc:{top_col}">{head}'
                      f'<div class="ms-body">{body}</div></div>')
    loc_txt = ", ".join(locs) if locs else "—"
    return (f'{css}<div class="ms-wrap"><div class="ms-head">'
            f'<div class="ms-av">{initials}</div>'
            f'<div><div class="ms-name">{nm}</div>'
            f'<div class="ms-sub">{et.title()}-time · Branches: <b>{loc_txt}</b></div></div>'
            f'<div class="ms-kpis">'
            f'<div class="ms-kpi"><div class="v">{len(mine)}</div><div class="l">Shifts</div></div>'
            f'<div class="ms-kpi"><div class="v">{total}</div><div class="l">Hours</div></div>'
            f'<div class="ms-kpi"><div class="v">{days_worked}</div><div class="l">Days on</div></div>'
            f'<div class="ms-kpi o"><div class="v">{rest}</div><div class="l">Rest</div></div>'
            f'</div></div>'
            f'<div class="ms-week">{cards}</div></div>')


def shift_board_html(sched, loc):
    color = LOC_COLORS.get(loc, "#37D7D0")
    order = ["Morning", "Mid", "Night"]
    css = """<style>
    .sb{background:linear-gradient(165deg,rgba(22,44,52,.92),rgba(15,32,38,.92));border:1px solid rgba(80,190,180,.16);border-radius:20px;padding:16px 18px;margin:10px 0 20px;box-shadow:0 12px 34px rgba(0,0,0,.34);}
    .sb-h{display:flex;align-items:center;gap:11px;font-family:'Baloo 2',cursive;font-weight:800;font-size:21px;color:#EAF3F1;margin-bottom:14px;}
    .sb-dot{width:13px;height:13px;border-radius:50%;box-shadow:0 0 12px 2px currentColor;}
    .sb-grid{display:grid;grid-template-columns:104px repeat(7,1fr);gap:7px;align-items:stretch;}
    .sb-dh{text-align:center;padding:7px 2px;border-radius:10px;background:rgba(55,215,208,.10);}
    .sb-dh.wknd{background:rgba(242,160,61,.16);}
    .sb-dow{font-size:10px;font-weight:800;color:#37D7D0;letter-spacing:.6px;display:block;text-transform:uppercase;}
    .sb-dh.wknd .sb-dow{color:#F2B96B;}
    .sb-dn{font-family:'JetBrains Mono',monospace;font-size:15px;font-weight:700;color:#fff;}
    .sb-lbl{display:flex;align-items:center;justify-content:center;font-weight:800;font-size:12.5px;border-radius:12px;border-left:5px solid var(--sc);background:rgba(255,255,255,.05);color:#EAF3F1;text-align:center;padding:6px;}
    .sb-cell{background:rgba(9,20,24,.5);border:1px solid rgba(120,200,190,.08);border-radius:12px;padding:7px 6px;min-height:56px;display:flex;flex-direction:column;gap:6px;justify-content:center;}
    .sb-chip{display:flex;align-items:center;gap:7px;}
    .sb-b{flex:0 0 auto;width:27px;height:27px;border-radius:9px;color:#0c1a20;font-weight:800;font-size:11px;display:flex;align-items:center;justify-content:center;font-family:'Baloo 2';box-shadow:0 2px 6px rgba(0,0,0,.4);}
    .sb-nm{font-size:11.5px;color:#EAF3F1;font-weight:600;line-height:1.1;}
    .sb-tt{font-family:'JetBrains Mono',monospace;font-size:9.5px;color:#8AA6A0;}
    .sb-none{color:#37504A;font-size:18px;text-align:center;}
    </style>"""
    hdr = '<div class="sb-lbl" style="background:transparent;border:none"></div>'
    for d in DATES:
        wknd = " wknd" if d.weekday() >= 5 else ""
        hdr += (f'<div class="sb-dh{wknd}"><span class="sb-dow">{d.strftime("%a")}</span>'
                f'<span class="sb-dn">{d.strftime("%d")}</span></div>')
    rows = ""
    for sh in order:
        loc_sh = sched[(sched.location == loc) & (sched["shift"] == sh)]
        if loc_sh.empty:
            continue
        col = SHIFT_COLORS.get(sh, "#888")
        ic = SHIFT_ICON.get(sh, "")
        rows += f'<div class="sb-lbl" style="--sc:{col}">{ic} {sh}</div>'
        for d in DATES:
            diso = d.isoformat()
            cell = loc_sh[loc_sh.date == diso].sort_values("start")
            if cell.empty:
                rows += '<div class="sb-cell"><span class="sb-none">·</span></div>'
                continue
            chips = ""
            for _, r in cell.iterrows():
                nm = str(r["employee"]).strip()
                ii = (nm[0] + (nm[1] if len(nm) > 1 else "")).upper()
                pt = " ⏳" if r["type"] == "part" else ""
                pin = " ⭐" if r["status"] == "pinned" else ""
                chips += (f'<div class="sb-chip" title="{nm} · {r["start"]}-{r["end"]}">'
                          f'<span class="sb-b" style="background:{col}">{ii}</span>'
                          f'<span><span class="sb-nm">{nm}{pt}{pin}</span><br>'
                          f'<span class="sb-tt">{r["start"]}-{r["end"]}</span></span></div>')
            rows += f'<div class="sb-cell">{chips}</div>'
    return (f'{css}<div class="sb"><div class="sb-h">'
            f'<span class="sb-dot" style="color:{color};background:{color}"></span>'
            f'{LOC_EMOJI.get(loc, "📍")} {loc_label(loc)}</div>'
            f'<div class="sb-grid">{hdr}{rows}</div></div>')


def dashboard_html(sched):
    today = now_myt().date()
    diso_list = [d.isoformat() for d in DATES]
    real = today.isoformat() in diso_list
    today_iso = today.isoformat() if real else diso_list[0]
    today_d = datetime.strptime(today_iso, "%Y-%m-%d")
    label = ("Today · " if real else "Week start · ") + today_d.strftime("%a %d %b")
    staff_total = len(employees)
    on_today = sched[sched.date == today_iso]["employee"].nunique()
    shifts_week = len(sched)
    hours_week = int(round(pd.to_numeric(sched["hours"], errors="coerce").fillna(0).sum(), 0))
    order = ["Morning", "Mid", "Night"]
    css = """<style>
    .db-kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:6px 0 6px;}
    .db-kpi{background:linear-gradient(160deg,rgba(19,38,48,.95),rgba(22,48,58,.95));border:1px solid rgba(80,190,180,.16);border-radius:18px;padding:15px 18px;box-shadow:0 10px 28px rgba(0,0,0,.3);}
    .db-kpi .ic{font-size:22px;}
    .db-kpi .v{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:31px;color:#fff;line-height:1;margin-top:8px;}
    .db-kpi .l{font-size:12px;color:#8AA6A0;margin-top:6px;font-weight:600;}
    .db-kpi.on{border-color:rgba(55,215,208,.5);box-shadow:0 10px 28px rgba(55,215,208,.14);}
    .db-kpi.on .v{color:#37D7D0;}
    .db-sec{font-family:'Baloo 2',cursive;font-weight:800;font-size:16px;color:#CFE3DE;margin:20px 0 11px;letter-spacing:.4px;}
    .db-today{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;}
    .db-loc{background:linear-gradient(165deg,rgba(22,44,52,.92),rgba(15,32,38,.92));border:1px solid rgba(80,190,180,.16);border-radius:18px;padding:14px 16px;box-shadow:0 8px 24px rgba(0,0,0,.28);}
    .db-loc-h{display:flex;align-items:center;gap:8px;font-family:'Baloo 2',cursive;font-weight:800;font-size:16px;color:#EAF3F1;margin-bottom:13px;}
    .db-cov{font-size:10.5px;font-weight:800;padding:2px 10px;border-radius:999px;margin-left:auto;}
    .db-cov.ok{background:rgba(46,200,120,.2);color:#67E0A3;}
    .db-cov.warn{background:rgba(242,160,61,.22);color:#F2C070;}
    .db-cov.none{background:rgba(226,59,46,.22);color:#F29A90;}
    .db-line{display:flex;align-items:flex-start;gap:9px;margin-bottom:11px;}
    .db-sh{flex:0 0 auto;font-size:10px;font-weight:800;padding:4px 9px;border-radius:999px;text-transform:uppercase;letter-spacing:.4px;white-space:nowrap;}
    .db-ppl{display:flex;flex-wrap:wrap;gap:6px;}
    .db-p{display:flex;align-items:center;gap:5px;background:rgba(255,255,255,.05);border:1px solid rgba(120,200,190,.1);border-radius:999px;padding:2px 10px 2px 3px;font-size:12px;color:#EAF3F1;font-weight:600;}
    .db-b{width:22px;height:22px;border-radius:7px;color:#0c1a20;font-weight:800;font-size:10px;display:flex;align-items:center;justify-content:center;font-family:'Baloo 2';}
    .db-empty{color:#5B726C;font-size:12.5px;font-style:italic;padding:6px 0;}
    .db-matrix{display:grid;grid-template-columns:104px repeat(7,1fr);gap:6px;}
    .db-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;}
    .db-mh{text-align:center;font-size:11px;font-weight:700;color:#9FB6B0;padding:5px 2px;line-height:1.3;}
    .db-mh.wknd{color:#F2B96B;}
    .db-ml{display:flex;align-items:center;gap:6px;font-size:12.5px;font-weight:700;color:#CFE3DE;padding:0 4px;}
    .db-cell{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;border-radius:11px;padding:9px 0;color:#fff;}
    .db-cico{font-size:17px;line-height:1;}
    .db-cnum{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:16px;}
    .db-cell.ok{background:rgba(46,200,120,.22);border:1px solid rgba(46,200,120,.3);}
    .db-cell.warn{background:rgba(242,160,61,.2);border:1px solid rgba(242,160,61,.32);color:#F2C070;}
    .db-cell.none{background:rgba(226,59,46,.13);border:1px solid rgba(226,59,46,.24);color:#E8938A;}
    .db-note{color:#8AA6A0;font-size:11.5px;margin-top:11px;}
    </style>"""
    kpis = ('<div class="db-kpis">'
            f'<div class="db-kpi"><div class="ic">👥</div><div class="v">{staff_total}</div><div class="l">Staff on roster</div></div>'
            f'<div class="db-kpi on"><div class="ic">🟢</div><div class="v">{on_today}</div><div class="l">On duty · {today_d.strftime("%a %d %b")}</div></div>'
            f'<div class="db-kpi"><div class="ic">🗓️</div><div class="v">{shifts_week}</div><div class="l">Shifts this week</div></div>'
            f'<div class="db-kpi"><div class="ic">⏱️</div><div class="v">{hours_week}</div><div class="l">Scheduled hours</div></div>'
            '</div>')
    tp = ""
    for loc in config["locations"]:
        day = sched[(sched.location == loc) & (sched.date == today_iso)]
        full = day[day.type == "full"]
        m = len(full[full["shift"] == "Morning"])
        n = len(full[full["shift"] == "Night"])
        if m >= 1 and n >= 1:
            badge = '<span class="db-cov ok">✓ Covered</span>'
        elif (m + n) > 0:
            badge = '<span class="db-cov warn">⚠ Partial</span>'
        else:
            badge = '<span class="db-cov none">✕ No staff</span>'
        lines = ""
        for sh in order:
            ss = day[day["shift"] == sh].sort_values("start")
            if ss.empty:
                continue
            col = SHIFT_COLORS.get(sh, "#888")
            ppl = ""
            for _, r in ss.iterrows():
                nm = str(r["employee"]).strip()
                ii = (nm[0] + (nm[1] if len(nm) > 1 else "")).upper()
                pt = " ⏳" if r["type"] == "part" else ""
                ppl += (f'<span class="db-p"><span class="db-b" style="background:{col}">{ii}</span>{nm}{pt}</span>')
            lines += (f'<div class="db-line"><span class="db-sh" style="background:{col}26;color:{col}">'
                      f'{SHIFT_ICON.get(sh, "")} {sh}</span><div class="db-ppl">{ppl}</div></div>')
        if not lines:
            lines = '<div class="db-empty">— no staff scheduled —</div>'
        tp += (f'<div class="db-loc"><div class="db-loc-h">{LOC_EMOJI.get(loc, "📍")} '
               f'{loc_label(loc)} {badge}</div>{lines}</div>')
    hdr = '<div class="db-ml"></div>'
    for d in DATES:
        wknd = " wknd" if d.weekday() >= 5 else ""
        hdr += f'<div class="db-mh{wknd}">{d.strftime("%a")}<br><b>{d.strftime("%d")}</b></div>'
    mrows = ""
    for loc in config["locations"]:
        mrows += f'<div class="db-ml">{LOC_EMOJI.get(loc, "📍")} {loc}</div>'
        for d in DATES:
            diso = d.isoformat()
            day = sched[(sched.location == loc) & (sched.date == diso)]
            full = day[day.type == "full"]
            m = len(full[full["shift"] == "Morning"])
            n = len(full[full["shift"] == "Night"])
            cnt = day["employee"].nunique()
            cls = "ok" if (m >= 1 and n >= 1) else ("warn" if cnt > 0 else "none")
            cico = {"ok": "🧑‍🍳", "warn": "⚠️", "none": "💤"}[cls]
            mrows += f'<div class="db-cell {cls}"><span class="db-cico">{cico}</span><span class="db-cnum">{cnt}</span></div>'
    matrix = f'<div class="db-scroll"><div class="db-matrix">{hdr}{mrows}</div></div>'
    html = css + kpis
    html += '<div class="db-sec">🟢 On Duty — ' + label + '</div>'
    html += '<div class="db-today">' + tp + '</div>'
    html += '<div class="db-sec">🗺️ Week Coverage — full-time morning &amp; night per branch</div>'
    html += matrix
    html += '<div class="db-note">🧑‍🍳 morning &amp; night both covered · ⚠️ partial · 💤 none · number = staff on duty that day</div>'
    return html


def render_assign_form():
    st.markdown("<div class='wd-compose-h'>➕ Assign or edit a shift</div>",
                unsafe_allow_html=True)
    st.caption("Pick the employee first — the **Location** list then shows only the "
               "branches that person is cleared to work.")
    c1, c2, c3 = st.columns(3)
    with c1:
        emp = st.selectbox("Employee", list(employees["name"]), key="asg_emp")
    etype = emp_type(emp)
    elig = eligible_locations(emp)
    with c2:
        loc = st.selectbox("Location (eligible only)",
                           [loc_label(l) for l in elig], key=f"asg_loc_{emp}")
        loc_key = elig[[loc_label(l) for l in elig].index(loc)]
    with c3:
        diso = st.selectbox("Date", [d.isoformat() for d in DATES],
                            format_func=lambda x: datetime.strptime(x, "%Y-%m-%d").strftime("%a %d %b"),
                            key="asg_date")
    shift_defs = config["part_shifts"] if etype == "part" else config["full_shifts"]
    c4, c5, c6 = st.columns(3)
    with c4:
        shift = st.selectbox("Shift", list(shift_defs.keys()), key="asg_shift")
    std = shift_defs[shift]
    with c5:
        start_t = st.time_input("Start", value=parse_hhmm(std["start"], time(10, 0)),
                                key=f"asg_start_{etype}_{shift}", step=1800)
    with c6:
        end_t = st.time_input("End", value=parse_hhmm(std["end"], time(19, 0)),
                              key=f"asg_end_{etype}_{shift}", step=1800)
    hrs = calc_hours(start_t, end_t)
    st.caption(f"Type: **{etype}** · duration: **{hrs} h** "
               f"{'(overnight)' if end_t <= start_t else ''}")
    note = st.text_input("Note (optional)", key="asg_note",
                         placeholder="e.g. requested night shift")
    pinned = st.checkbox("📌 Fixed request (pin — protect from auto-generate)", key="asg_pin")
    b1, _ = st.columns([1, 3])
    if b1.button("➕ Add / update shift", type="primary", width="stretch"):
        df = st.session_state.schedule
        srow = {"date": diso, "day": day_name(diso), "location": loc_key, "shift": shift,
                "employee": emp, "type": etype,
                "start": start_t.strftime("%H:%M"), "end": end_t.strftime("%H:%M"),
                "hours": hrs, "status": "pinned" if pinned else "manual",
                "note": note, "clock_in": "", "clock_out": ""}
        mask = ((df.employee == emp) & (df.date == diso) &
                (df.location == loc_key) & (df["shift"] == shift))
        if mask.any():
            for k, v in srow.items():
                df.loc[mask, k] = v
            msg = "Shift updated."
        else:
            df = pd.concat([df, pd.DataFrame([srow])], ignore_index=True)
            msg = "Shift added."
        st.session_state.schedule = df[SCHED_COLS]
        save_schedule(st.session_state.schedule)
        st.success(f"{msg}  {emp} → {loc_label(loc_key)}, {shift} "
                   f"{start_t.strftime('%H:%M')}-{end_t.strftime('%H:%M')} on {diso}")
        st.rerun()
    df = st.session_state.schedule
    existing = df[(df.employee == emp) & (df.date == diso)]
    if not existing.empty:
        st.caption(f"{emp}'s shifts on {diso}:")
        for idx, r in existing.iterrows():
            cc = st.columns([4, 1])
            cc[0].write(f"• {loc_label(r['location'])} · {r['shift']} · {r['start']}-{r['end']}")
            if cc[1].button("🗑️ Remove", key=f"del_{idx}"):
                st.session_state.schedule = df.drop(idx).reset_index(drop=True)
                save_schedule(st.session_state.schedule)
                st.rerun()


def render_admin():
    tab_sched, tab_clock, tab_perf, tab_setup, tab_settings = st.tabs(
        ["📅 Schedule", "⏱️ Clock In / Out", "📊 Performance", "⚙️ Setup", "🔑 Settings"])

    with tab_sched:
        # ---- Week context bar (which week am I arranging?) ----
        today = now_myt().date()
        this_mon = today - timedelta(days=today.weekday())
        we = _ws + timedelta(days=6)
        dw = (_ws - this_mon).days // 7
        if dw == 0:
            rel_txt, rel_cls = "This week", "now"
        elif dw == 1:
            rel_txt, rel_cls = "Next week", "next"
        elif dw < 0:
            rel_txt, rel_cls = ("Last week" if dw == -1 else f"{-dw} weeks ago"), "past"
        else:
            rel_txt, rel_cls = f"In {dw} weeks", "next"
        st.markdown(
            "<div class='wd-abar'><span class='wd-abar-ico'>🗓️</span>"
            "<div class='wd-abar-txt'>Arranging schedule"
            f"<small>{_ws.strftime('%d %b')} – {we.strftime('%d %b %Y')}</small></div>"
            f"<span class='wd-abar-pill {rel_cls}'>{rel_txt}</span></div>",
            unsafe_allow_html=True)
        # ---- Quick jumps (coming week emphasised) ----
        j1, j2, j3 = st.columns(3)
        if j1.button("📍 This week", width="stretch", key="jmp_this"):
            st.session_state.week_start_iso = this_mon.isoformat()
            st.rerun()
        if j2.button("➡️ Next week (arrange)", type="primary",
                     width="stretch", key="jmp_next"):
            st.session_state.week_start_iso = (this_mon + timedelta(days=7)).isoformat()
            st.rerun()
        if j3.button("◀ Previous week", width="stretch", key="jmp_prev"):
            st.session_state.week_start_iso = (_ws - timedelta(days=7)).isoformat()
            st.rerun()
        render_week_nav("admin")

        # ---- On-leave (off days) editor for the viewed week ----
        wk_off = {d: config.get("off_days", {}).get(d, []) for d in WEEK_ISO}
        n_off_wk = sum(len(v) for v in wk_off.values())
        with st.expander(f"🛌 On leave this week — {n_off_wk} entr"
                         f"{'y' if n_off_wk == 1 else 'ies'} (add / remove)",
                         expanded=False):
            st.caption("Saved instantly for everyone — the staff Schedule leave board, "
                       "Setup tables and Auto-generate all use this list.")
            for diso in WEEK_ISO:
                names = wk_off.get(diso, [])
                d = datetime.strptime(diso, "%Y-%m-%d")
                cols = st.columns([1.1] + [1] * max(1, len(names)) + [2])
                cols[0].markdown(f"**{d.strftime('%a %d %b')}**")
                if not names:
                    cols[1].caption("— nobody off —")
                for i, nm in enumerate(names):
                    if cols[1 + i].button(f"🛌 {nm} ✕", key=f"offrm_{diso}_{nm}",
                                          help=f"Remove {nm}'s off day on {diso}"):
                        ok, err = offday_remove(diso, nm)
                        if ok:
                            st.toast(f"Removed {nm}'s off day ({diso}).")
                            st.rerun()
                        else:
                            st.error(f"Could not remove: {err}")
            a1, a2, a3 = st.columns([1.4, 1.4, 1])
            add_day = a1.selectbox(
                "Day", WEEK_ISO,
                format_func=lambda x: datetime.strptime(x, "%Y-%m-%d").strftime("%a %d %b"),
                key="off_add_day")
            add_emp = a2.selectbox("Staff", list(employees["name"]), key="off_add_emp")
            a3.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            if a3.button("➕ Add off day", type="primary", key="off_add_btn"):
                if add_emp in config.get("off_days", {}).get(add_day, []):
                    st.info(f"{add_emp} is already off on {add_day}.")
                else:
                    ok, err = offday_add(add_day, add_emp)
                    if ok:
                        st.toast(f"{add_emp} is now off on {add_day}. 🛌")
                        st.rerun()
                    else:
                        st.error(f"Could not add: {err}")
            st.caption("⚠️ Adding an off day does not remove already-assigned shifts on "
                       "that day — check the grid below and remove any clashing shift.")

        c1, c2, c3, c4 = st.columns([1.6, 1.3, 1, 1.4])
        with c1:
            if st.button("⚡ Auto-generate THIS week", type="primary", width="stretch"):
                rows = sch.generate_schedule(employees, config)
                new = pd.DataFrame(rows)[SCHED_COLS] if rows else pd.DataFrame(columns=SCHED_COLS)
                keep = st.session_state.schedule[~st.session_state.schedule.date.isin(WEEK_ISO)]
                st.session_state.schedule = pd.concat([keep, new], ignore_index=True)[SCHED_COLS]
                save_schedule(st.session_state.schedule)
                st.success(f"Generated {len(new)} shifts for {WEEK_ISO[0]} → {WEEK_ISO[-1]}.")
        with c2:
            prev_iso = [(_ws - timedelta(days=7) + timedelta(days=i)).isoformat() for i in range(7)]
            if st.button("📋 Copy previous week", width="stretch",
                         help="Seed this week from last week's arrangement, then tweak."):
                src = st.session_state.schedule[
                    st.session_state.schedule.date.isin(prev_iso)].copy()
                if src.empty:
                    st.warning("Previous week has no shifts to copy from.")
                else:
                    dmap = {prev_iso[i]: WEEK_ISO[i] for i in range(7)}
                    src["date"] = src["date"].map(dmap)
                    src["day"] = src["date"].map(day_name)
                    src["clock_in"] = ""
                    src["clock_out"] = ""
                    keep = st.session_state.schedule[
                        ~st.session_state.schedule.date.isin(WEEK_ISO)]
                    st.session_state.schedule = pd.concat(
                        [keep, src], ignore_index=True)[SCHED_COLS]
                    save_schedule(st.session_state.schedule)
                    st.success(f"Copied {len(src)} shifts into {WEEK_ISO[0]} → {WEEK_ISO[-1]}.")
                    st.rerun()
        with c3:
            if st.button("🗑️ Clear week", width="stretch"):
                st.session_state.schedule = st.session_state.schedule[
                    ~st.session_state.schedule.date.isin(WEEK_ISO)].reset_index(drop=True)
                save_schedule(st.session_state.schedule)
                st.rerun()
        with c4:
            st.download_button("⬇️ Download all CSV",
                               st.session_state.schedule.to_csv(index=False),
                               file_name="schedule.csv", width="stretch")
        st.caption("Build the coming week fast: **Next week → Copy previous week** (or "
                   "**Auto-generate**), then fine-tune below.")
        st.divider()
        render_assign_form()
        st.divider()
        sweek = st.session_state.schedule[st.session_state.schedule.date.isin(WEEK_ISO)]
        if sweek.empty:
            st.info("No shifts for this week yet. Use **Auto-generate THIS week** or the assign form above.")
        else:
            gaps = sch.find_gaps(sweek.to_dict("records"), config)
            if gaps:
                st.warning("Coverage gaps (unfilled full-time slots): " +
                           ", ".join(f"{g[0]} {g[1]} {g[2]} (need {g[3]})" for g in gaps))
            else:
                st.success("✅ All required full-time shifts are covered this week.")
            view = st.radio("View", ["Weekly grid", "Edit table"], horizontal=True)
            if view == "Weekly grid":
                loc_filter = st.multiselect("Filter location", config["locations"],
                                            default=config["locations"], format_func=loc_label)
                sweek_ck = overlay_checkins(sweek)  # show ✓ on checked-in staff
                for loc in config["locations"]:
                    if loc not in loc_filter:
                        continue
                    st.markdown(grid_html(sweek_ck, loc), unsafe_allow_html=True)
            else:
                st.caption("Bulk-edit THIS week's shifts. The assign form above enforces "
                           "per-employee eligible locations; this table lets you edit freely.")
                edited = st.data_editor(
                    sweek, num_rows="dynamic", width="stretch", hide_index=True,
                    column_config={
                        "date": st.column_config.SelectboxColumn(
                            "Date", options=WEEK_ISO),
                        "location": st.column_config.SelectboxColumn(
                            "Location", options=config["locations"]),
                        "shift": st.column_config.SelectboxColumn(
                            "Shift", options=list(config["full_shifts"].keys())),
                        "employee": st.column_config.SelectboxColumn(
                            "Employee", options=list(employees["name"])),
                        "type": st.column_config.SelectboxColumn(
                            "Type", options=["full", "part"]),
                        "hours": st.column_config.NumberColumn("Hours", min_value=0, max_value=16),
                    }, key="sched_editor")
                other = st.session_state.schedule[~st.session_state.schedule.date.isin(WEEK_ISO)]
                st.session_state.schedule = pd.concat([other, edited], ignore_index=True)[SCHED_COLS]

    with tab_clock:
        st.subheader("Clock In / Out")
        st.caption("Stamp current Sabah time with one click, or type it. Format: YYYY-MM-DD HH:MM")
        st.caption("🟢 Supabase — check-ins saved to the cloud database (survive redeploys)."
                   if db_enabled() else
                   "⚪ Local CSV — check-ins are temporary until Supabase secrets are added.")
        sched = st.session_state.schedule
        if sched.empty:
            st.info("Generate a schedule first.")
        else:
            d_sel = st.selectbox("Day", [d.isoformat() for d in DATES],
                                 format_func=lambda x: datetime.strptime(x, "%Y-%m-%d").strftime("%a %d %b"))
            day_rows = overlay_checkins(sched[sched.date == d_sel])
            if day_rows.empty:
                st.info("No shifts on this day.")
            st.caption("📍 = staff GPS self-check-in (distance from branch centre shown) · "
                       "⏰ = minutes late vs shift start.")
            for idx, r in day_rows.iterrows():
                cols = st.columns([2.4, 1.4, 1.4, 1, 1])
                gps = ""
                if str(r.get("ci_method", "")) == "gps":
                    gps = f" · 📍GPS {r.get('ci_dist','?')}m"
                lm = late_minutes(r["clock_in"], r["start"]) if str(r["clock_in"]).strip() else None
                if lm is not None:
                    gps += f" · ⏰{lm}m late" if lm > 0 else " · ✅on time"
                cols[0].markdown(f"**{r['employee']}** · {SHIFT_ICON.get(r['shift'],'')} {r['shift']} "
                                 f"· 📍{loc_label(r['location'])} · {r['start']}-{r['end']}{gps}")
                ci = cols[1].text_input("Clock in", value=r["clock_in"], key=f"ci_{idx}",
                                        label_visibility="collapsed", placeholder="clock in")
                co = cols[2].text_input("Clock out", value=r["clock_out"], key=f"co_{idx}",
                                        label_visibility="collapsed", placeholder="clock out")
                if not db_enabled():
                    st.session_state.schedule.at[idx, "clock_in"] = ci
                    st.session_state.schedule.at[idx, "clock_out"] = co
                if cols[3].button("🟢 In now", key=f"cin_{idx}"):
                    stamp = now_myt().strftime("%Y-%m-%d %H:%M")
                    if db_enabled():
                        ok, err = db_upsert_checkin(_manual_record(r, stamp))
                        st.toast("Saved" if ok else f"Save failed: {err}")
                    else:
                        st.session_state.schedule.at[idx, "clock_in"] = stamp
                        save_schedule(st.session_state.schedule)
                    st.rerun()
                if cols[4].button("🔴 Out now", key=f"cout_{idx}"):
                    st.session_state.schedule.at[idx, "clock_out"] = now_myt().strftime("%Y-%m-%d %H:%M")
                    save_schedule(st.session_state.schedule)
                    st.rerun()
            if st.button("💾 Save clock records", type="primary"):
                if db_enabled():
                    saved = 0
                    for idx, r in day_rows.iterrows():
                        val = st.session_state.get(f"ci_{idx}", "").strip()
                        if val:
                            ok, _ = db_upsert_checkin(_manual_record(r, val))
                            saved += 1 if ok else 0
                    st.success(f"Saved {saved} check-in(s) to Supabase.")
                else:
                    save_schedule(st.session_state.schedule)
                    st.success("Clock records saved.")

    with tab_perf:
        st.subheader("Employee Performance")
        sched = st.session_state.schedule
        if sched.empty:
            st.info("Generate a schedule first.")
        else:
            df = overlay_checkins(sched)
            df["hours"] = pd.to_numeric(df["hours"], errors="coerce").fillna(0)
            df["actual"] = df.apply(lambda r: actual_hours(r["clock_in"], r["clock_out"]), axis=1)
            rows = []
            for name in employees["name"]:
                e = df[df.employee == name]
                if e.empty:
                    continue
                rows.append({
                    "Employee": name, "Type": e["type"].iloc[0],
                    "Days worked": e["date"].nunique(), "Shifts": len(e),
                    "Scheduled hrs": round(e["hours"].sum(), 1),
                    "Actual hrs (clocked)": round(e["actual"].dropna().sum(), 1),
                    "Locations": ", ".join(sorted(loc_label(x) for x in e["location"].unique())),
                })
            perf = pd.DataFrame(rows).sort_values("Scheduled hrs", ascending=False)
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Employees scheduled", perf["Employee"].nunique())
            k2.metric("Total shifts", int(perf["Shifts"].sum()))
            k3.metric("Total scheduled hrs", round(perf["Scheduled hrs"].sum(), 1))
            k4.metric("Total clocked hrs", round(perf["Actual hrs (clocked)"].sum(), 1))
            st.dataframe(perf, width="stretch", hide_index=True)
            st.bar_chart(perf.set_index("Employee")["Scheduled hrs"])
            st.markdown("##### Hours by location")
            by_loc = df.groupby("location")["hours"].sum().reset_index()
            by_loc["location"] = by_loc["location"].map(loc_label)
            st.bar_chart(by_loc.set_index("location")["hours"])
            st.download_button("⬇️ Download performance CSV",
                               perf.to_csv(index=False), file_name="performance.csv")

    with tab_setup:
        st.subheader("Employees, locations & rules")
        st.caption("These drive the auto-generator. Edit employees here; edit off-days / "
                   "shift requests in data/week_config.json for now.")
        st.markdown("##### 👥 Employees")
        st.caption("locations = semicolon separated (Aeropod;Lintas;Beverly) · "
                   "is_core = non-consecutive full days · no_off_day = works all 7 days")
        ed_emp = st.data_editor(employees, num_rows="dynamic", width="stretch",
                                hide_index=True, key="emp_editor")
        if st.button("💾 Save employees"):
            ed_emp.to_csv(EMP_CSV, index=False)
            st.cache_data.clear()
            st.success("Employees saved. Re-generate the schedule to apply.")
        colA, colB = st.columns(2)
        with colA:
            st.markdown("##### 🛌 Off-day applications")
            off = config.get("off_days", {})
            if off:
                st.table(pd.DataFrame([{"Date": k, "Off": ", ".join(v)} for k, v in off.items()]))
            else:
                st.caption("No off-day applications this week.")
            st.caption("No-off members: " + (", ".join(config.get("no_off_members", [])) or "—"))
        with colB:
            st.markdown("##### 📌 Shift requests")
            reqs = config.get("shift_requests", [])
            if reqs:
                rq = pd.DataFrame(reqs)
                for c in ["name", "date", "shift", "hard", "note"]:
                    if c not in rq.columns:
                        rq[c] = ""
                st.table(rq[["name", "date", "shift", "hard", "note"]])
            else:
                st.caption("No shift requests this week.")
        st.markdown("##### 🕒 Part-time availability")
        pa = config.get("part_availability", {})
        if pa:
            st.table(pd.DataFrame(pa).T)
        else:
            st.caption("No part-time availability set for this week.")
        st.markdown("##### 🗄️ Check-in storage")
        if db_enabled():
            st.success("🟢 **Supabase connected** — staff check-ins are saved to the cloud "
                       "database and persist across redeploys.")
        else:
            st.warning("⚪ **Local CSV (temporary).** Check-ins won't survive a redeploy until "
                       "the Supabase secrets are added. On Streamlit Cloud: **Manage app ▸ "
                       "Settings ▸ Secrets**, paste the `[supabase]` url + key, and reboot.")

        st.markdown("##### ⏳ Check-in time window")
        stt = load_settings()
        wc1, wc2 = st.columns([2, 1])
        new_win = wc1.number_input(
            "Early check-in window — minutes before shift start",
            min_value=0, max_value=120, step=5,
            value=int(stt.get("early_min", DEFAULT_EARLY_MIN)), key="early_min_setting")
        wc2.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
        if wc2.button("💾 Save window", key="save_early_min"):
            stt["early_min"] = int(new_win)
            save_settings(stt)
            st.success(f"Check-in now opens {int(new_win)} min before each shift start.")
            st.rerun()
        st.caption("Staff can check in from this many minutes before their shift starts. "
                   "Late check-ins are always allowed and recorded with minutes late.")

        st.markdown("##### 📍 Branch check-in geofences (GPS)")
        st.caption("Stand at each branch, tap **Get my current GPS**, type the numbers into that "
                   "branch's fields, set the radius (30 m = at the shop with room for normal "
                   "GPS drift; raise it if staff get false 'too far' rejections indoors), "
                   "then **Save**. Staff can only "
                   "check in within this radius. Coordinates ship as placeholders — set the real "
                   "ones on-site before going live.")
        sites = load_sites()
        components.html(geo_show_html(), height=120)
        for loc in config["locations"]:
            s = sites.get(loc, {})
            flag = "🟢 set" if s.get("configured") else "⚪ not set — check-in disabled"
            st.markdown(f"**{LOC_EMOJI.get(loc, '📍')} {loc_label(loc)}** &nbsp; {flag}")
            cc = st.columns([1.3, 1.3, 1, 1])
            lat = cc[0].number_input("Latitude", value=float(s.get("lat", 5.98)),
                                     format="%.6f", key=f"slat_{loc}")
            lng = cc[1].number_input("Longitude", value=float(s.get("lng", 116.07)),
                                     format="%.6f", key=f"slng_{loc}")
            rad = cc[2].number_input("Radius (m)", value=int(s.get("radius_m", 20)),
                                     min_value=10, max_value=500, step=10, key=f"srad_{loc}")
            cc[3].markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            if cc[3].button("💾 Save", key=f"ssave_{loc}"):
                sites[loc] = {"lat": float(lat), "lng": float(lng),
                              "radius_m": int(rad), "configured": True}
                save_sites(sites)
                st.success(f"{loc_label(loc)} check-in location saved (radius {int(rad)} m).")
                st.rerun()

        st.markdown("##### ⏰ Shift definitions")
        fs = pd.DataFrame(config["full_shifts"]).T.reset_index().rename(columns={"index": "Full shift"})
        ps = pd.DataFrame(config["part_shifts"]).T.reset_index().rename(columns={"index": "Part shift"})
        c1, c2 = st.columns(2)
        c1.table(fs)
        c2.table(ps)

    with tab_settings:
        st.subheader("🔑 Admin settings")
        a = load_admin()
        st.write(f"Current admin username: **{a['user']}**")
        st.markdown("##### Change password")
        cur = st.text_input("Current password", type="password", key="pw_cur")
        new1 = st.text_input("New password", type="password", key="pw_new1")
        new2 = st.text_input("Confirm new password", type="password", key="pw_new2")
        new_user = st.text_input("Change username (optional)", value=a["user"], key="pw_user")
        if st.button("Update credentials", type="primary"):
            if not verify_admin(a["user"], cur):
                st.error("Current password is incorrect.")
            elif new1 == "" or new1 != new2:
                st.error("New passwords are empty or do not match.")
            else:
                save_admin({"user": new_user.strip() or a["user"], "hash": _hash(new1)})
                st.success("Credentials updated. Use them next time you log in.")
        st.info("Note: on Streamlit Community Cloud the file system resets on redeploy, "
                "so the password reverts to default after a redeploy.")


def render_login_gate():
    st.markdown(
        "<div class='wd-gate'>"
        "<div class='wd-gate-lock'>🔐</div>"
        "<div class='wd-gate-title'>Admin Access</div>"
        "<div class='wd-gate-sub'>Sign in to build & edit the shift schedule</div>"
        "</div>",
        unsafe_allow_html=True)
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        with st.form("admin_login", clear_on_submit=False):
            u = st.text_input("Username", value="", key="gate_user",
                              placeholder="admin")
            p = st.text_input("Password", value="", type="password", key="gate_pw",
                              placeholder="••••••••")
            ok = st.form_submit_button("🔓  Log in", type="primary",
                                       width="stretch")
        if ok:
            if verify_admin(u, p):
                st.session_state.is_admin = True
                st.rerun()
            else:
                st.error("Wrong username or password. Please try again.")
        st.markdown(
            "<div class='wd-gate-hint'>First time? Default login is "
            "<code>admin</code> / <code>wedrink2026</code> — "
            "change it under <b>Settings</b> after signing in.</div>",
            unsafe_allow_html=True)
    st.divider()
    with st.expander("👀 Preview the read-only staff schedule (no login needed)"):
        render_overall()


try:
    if view_mode == "🔐 Admin" and IS_ADMIN:
        render_admin()
    elif view_mode == "🔐 Admin" and not IS_ADMIN:
        render_login_gate()
    else:
        render_overall()
except Exception as _e:                          # show real error, never "Oh no"
    st.error(f"This page hit an error (build {APP_BUILD}) — details below. "
             "Other pages still work; please screenshot this for the admin.")
    st.exception(_e)

st.markdown(
    "<div style='text-align:center;color:#9BB0AA;font-size:12px;padding:22px 0 8px;'>"
    f"🧋 WeDrink Sabah · Shift Dashboard — Aeropod · Lintas Plaza · Beverly Hills · {APP_BUILD}"
    "</div>", unsafe_allow_html=True)
