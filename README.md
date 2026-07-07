# 🗓️ Shift Dashboard

Auto + semi-automatic staff scheduling for a 3-location operation in
**Kota Kinabalu, Sabah, Malaysia** (time zone `Asia/Kuching`, UTC+8).

Locations: **Aeropod · Lintas · Beverly**

## What it does

- **Auto-generate** a full week's schedule from your rules (locations each person can
  work, off-day applications, shift requests, part-time availability, core-member limits).
- **Semi-auto editing** — change any assignment: swap employee, move location, change the
  shift, or edit the start/end times. Add or delete shifts.
- **Location indication** — every shift is tagged and colour-coded by branch.
- **Clock In / Out** — stamp the current Sabah time with one click, or type the date & time
  manually. Handles overnight shifts (e.g. 15:00 → 00:00).
- **Performance tab** — working hours (scheduled vs actually clocked) and days worked per
  employee, plus hours by location. Downloadable as CSV.

## Two views

- **👀 Overall (staff)** — read-only. Anyone can open the app, see the weekly schedule
  (dashboard / calendar / shift board) and look up their own shifts. No login.
- **🔐 Admin** — pick *Admin* in the sidebar and you land on a branded **sign-in gate**.
  Default login `admin` / `wedrink2026` (change it under **Settings** after first login).
  Only admins can generate, edit, clock, and manage staff.

## Admin — arranging the coming week

The Schedule tab is built around planning the *next* week quickly:

1. **➡️ Next week (arrange)** — one click jumps the editor to next Monday. A context bar
   always shows which week you're on (*This week / Next week / Last week …*).
2. Seed it, then tweak:
   - **⚡ Auto-generate** a fresh week from the rules, **or**
   - **📋 Copy previous week** to reuse last week's arrangement as a starting point.
3. Fine-tune with the **Assign / edit a shift** card (employee-first — the Location list
   only offers branches that person is cleared for) or the free **Edit table**.
4. Coverage gaps for unfilled full-time slots are flagged automatically.

## Tabs (Admin)

| Tab | Purpose |
|-----|---------|
| 📅 Schedule | Week jumps, auto-generate / copy-week, assign card, grid & editable table |
| ⏱️ Clock In / Out | Per-shift clock in/out — "now" button or manual entry |
| 📊 Performance | Hours & days per employee, charts, CSV export |
| ⚙️ Setup | Edit employees; view off-days, requests, availability, shift times |
| 🔑 Settings | Change the admin username / password |

## Files

```
shift-dashboard/
├── app.py                 # Streamlit UI
├── scheduler.py           # constraint-aware auto-scheduler
├── requirements.txt
├── runtime.txt            # pins Python 3.11 for Streamlit Cloud
├── .streamlit/config.toml # theme
└── data/
    ├── employees.csv      # staff, eligible locations, core / no-off flags
    ├── week_config.json   # shifts, off-days, requests, PT availability
    └── schedule.csv       # current schedule + clock records (auto-created)
```

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```
Then open http://localhost:8501

## Deploy: GitHub → Streamlit Community Cloud

### 1. Put the code on GitHub
1. Create a free account at https://github.com if you don't have one.
2. Click **New repository** → name it `shift-dashboard` → keep it **Public** (Community
   Cloud needs read access; Private also works) → **Create repository**.
3. Upload the files — easiest way, no command line:
   - On the empty repo page click **uploading an existing file**.
   - Drag in **everything inside this folder** (app.py, scheduler.py, requirements.txt,
     runtime.txt, README.md, the `data/` folder and the `.streamlit/` folder).
   - Click **Commit changes**.

   Or with git on your computer:
   ```bash
   cd shift-dashboard
   git init
   git add .
   git commit -m "Shift Dashboard"
   git branch -M main
   git remote add origin https://github.com/<your-username>/shift-dashboard.git
   git push -u origin main
   ```

### 2. Deploy on Streamlit
1. Go to https://share.streamlit.io and sign in **with your GitHub account**.
2. Click **Create app** → **Deploy a public app from GitHub**.
3. Fill in:
   - **Repository:** `<your-username>/shift-dashboard`
   - **Branch:** `main`
   - **Main file path:** `app.py`
4. Click **Deploy**. First build takes ~2 minutes. You'll get a public URL like
   `https://<your-app>.streamlit.app` to share with your team.

### Updating later
Edit a file on GitHub (or `git push`) and Streamlit redeploys automatically.

## ⚠️ Data persistence note

This version stores data in CSV files inside the app. That's perfect for building and
fine-tuning, **but Streamlit Community Cloud resets the file system on every redeploy or
restart** — so clock-in records typed on the live site are not permanent there. Options:
- Use the **⬇️ Download CSV** buttons to keep records, or
- Ask to upgrade the storage to a database (Supabase / Google Sheets) for permanent,
  multi-user records. The code is structured so this is an easy next step.

## Scheduling rules encoded

- Each person is only assigned to locations they're cleared for.
- Off-day applications and hard shift requests are always respected.
- Core members are steered away from working full shifts on consecutive days.
- Non "no-off" staff get at least one rest day in the week.
- Part-timers are slotted only within their stated availability windows.

Edit these inputs in the **Setup** tab (employees) and `data/week_config.json`
(off-days, requests, availability), then re-generate.
