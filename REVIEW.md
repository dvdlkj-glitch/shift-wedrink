# 🗓️ Shift Dashboard — Review & Requirements Check

**Reviewed:** 5 Jul 2026 · **Week under test:** 2026-07-06 (Mon) → 2026-07-12 (Sun)
**App:** Streamlit · **Locations:** Aeropod · Lintas · Beverly · **Time zone:** Asia/Kuching (UTC+8)
**Run locally:** `streamlit run app.py` → http://localhost:8501

---

## 1. What the dashboard does

| Tab | Purpose | Status |
|-----|---------|--------|
| 📅 **Schedule** | One-click auto-generate for the whole week, colour-coded grid per branch (★ = pinned hard request, (PT) = part-timer), editable table view, CSV download | ✅ Works |
| ⏱️ **Clock In / Out** | Per-shift "In now / Out now" buttons stamp Sabah time (UTC+8), or type manually; handles overnight shifts (15:00→00:00) | ✅ Works |
| 📊 **Performance** | 14 staff, 59 shifts, 446 scheduled hrs; days / shifts / hours per person, scheduled vs clocked | ✅ Works |
| ⚙️ **Setup** | Employees editable in-app; off-days / shift requests via `data/week_config.json` | ✅ Works |

---

## 2. Requirements met ✅

Cross-checked against `排班人員的Condition 和 Info.txt`.

- **Off-day applications** — all honored:
  - Mon: Alya, Nier · Tue: Alya, Kelvin · Wed: Aggie, Eva · Thu: Murni, Yya · Fri: Wanna
- **Hard shift requests** — all pinned correctly (shown with ★):
  - Azz — Mon night · Murni — Fri night · Nier — Fri night & Sun night · Lela — Sun morning
  - Aggie's soft "prefers morning" is mostly honored (mornings Mon/Thu/Sat)
- **Location eligibility** — nobody is scheduled outside their allowed branch.
- **Part-time availability windows** — all respected:
  - Qiara (off Wed) · MeiMei (morning-only Thu–Sun, no-morning Wed) · Aaron (evenings Mon–Thu, off Fri, anytime Sat–Sun)
- **Coverage** — every branch has 1 morning + 1 night full-timer every day. **Zero gaps.**

---

## 3. Where it diverges from the rules ⚠️

### 3.1 "Core member cannot work 2 full days in a row" — NOT enforced (soft only)
The rule *"full time employee is not allowed to continue 2 days full time work"* is implemented as a **score penalty**, not a hard block (`scheduler.py:124`). Result — two violations in the generated week:

| Employee | Days worked | Problem |
|----------|-------------|---------|
| **Yya** (core) | Mon, **Tue**, Fri, Sun | Mon + Tue are consecutive |
| **Aggie** (core) | Mon, **Thu, Fri, Sat** | Thu + Fri + Sat = three in a row |

> **This is the main rule-breaker.** Fix = block consecutive full days for core members instead of just penalising them.

### 3.2 "No-off" members not given maximum work
Lela and Azz are marked *"don't want to take a day off"* but only receive **3 and 4 days**. The `no_off_day` flag currently means *"allowed to work 7 days,"* not *"schedule them every day."*

| Employee | Days worked | Expected (if flag = "work all 7") |
|----------|-------------|-----------------------------------|
| Lela | 3 | up to 7 |
| Azz | 4 | up to 7 |

### 3.3 Naming — "lela" vs "Layla"
The source text lists both **"lela"** (no-off, morning request) and **"Layla"** (core, Aero & Lintas). The data merged them into a single person **Lela** (Aeropod;Lintas, core, no-off). **Please confirm they are the same person.**

### 3.4 Minor
- `friday_conditional` (*"Friday off only approved when enough staff"*) is stored in config but no logic uses it.
- Off-days / shift requests are editable only by hand in `week_config.json`, not in the UI.
- Harmless `use_container_width` deprecation warnings from the newer Streamlit version.

---

## 4. Notes on running it (fixes already applied)

Two small changes were needed to launch the app cleanly on this machine:
1. **`data/` path anchored to the script** (`app.py`) — it previously only found its data when launched from inside its own folder.
2. **UTF-8 mode forced at launch** — Windows crashed on a Streamlit startup log line containing the Chinese folder name (`排班表`) under the default cp1252 console encoding.

---

## 5. Recommended next steps

1. **(Recommended)** Enforce "no 2 consecutive full days" as a **hard block** for core members → fixes Yya & Aggie.
2. Decide what `no_off_day` should mean — *"may work 7"* (current) or *"schedule all 7"* (Lela & Azz).
3. Confirm the **lela / Layla** identity.
4. Optional: enforce the Friday-conditional rule; move off-days / requests editing into the Setup UI.
5. For permanent multi-user clock records, move storage from CSV to a database (CSV resets on Streamlit Cloud redeploy).
