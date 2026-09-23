# 📅 Chalmers Calendar Sync

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

One Google Calendar that always matches your real schedule -- no more
checking Canvas and TimeEdit separately, or re-subscribing every term when
Google Calendar's own ICS import goes stale.

## 🛠️ Setup

1. **Canvas**: on the Canvas Calendar page, open the sidebar's "Calendar
   feed" link (bottom right, under the calendar list) and copy its ICS URL.

   ![Canvas calendar feed link](docs/canvas-calendar-feed.png)

2. **TimeEdit**: click **Prenumerera** ("Subscribe") and copy the ICS link
   from there -- not the one-off download button, since it needs to be
   fetchable with a plain HTTP GET.

   ![TimeEdit subscribe dialog](docs/timeedit-subscribe.png)

3. **Google Calendar**:
   - Create a dedicated calendar for this (Settings -> "Create new
     calendar"), so it's safe to wipe/resync without touching your main
     calendar. Put its ID (Settings -> that calendar -> "Calendar ID")
     in `.env` as `GOOGLE_CALENDAR_ID` -- not in `config.yaml`, since
     that's tracked in git and this value is personal.
   - In a Google Cloud project, enable the **Google Calendar API**.
   - This authenticates as *you*, via OAuth, rather than a service account
     -- Google now disables service-account key downloads by default on
     every new project (not specific to this project or Chalmers), so
     that path is a dead end. OAuth is actually simpler here anyway: since
     it's your own calendar under your own account, there's no separate
     "share the calendar with a service account" step.
     - APIs & Services -> OAuth consent screen: set it up as **External**,
       fill in the required fields, and add your own Google account under
       **Test users**.
     - APIs & Services -> Credentials -> Create Credentials -> **OAuth
       client ID** -> Application type **Desktop app**. Download the JSON
       and save it as `client_secret.json` in the repo root.
     - Run `python -m src.auth_setup` locally. It opens a browser to log
       in with the account that owns the calendar; since the app is
       unverified you'll need to click through "Advanced -> Go to
       (app name)". This writes `token.json`, which `src/main.py` reads by
       default.
4. Copy `.env.example` to `.env` and fill in the URLs above, or export the
   same variables in your shell.
5. `pip install -r requirements.txt`

## ▶️ Running

```bash
# Fetch + normalize + print, writes nothing:
python -m src.main --dry-run

# Actually sync to Google Calendar:
python -m src.main
```

Check the dry run output carefully before doing a real sync -- especially
new courses each term, since `_parse_summary` in `src/sources/timeedit.py`
was written against Chalmers' actual TimeEdit field layout but course
names still need adding to `config.yaml`'s `timeedit.translations` as they
show up (there's no general Swedish translator, just a fixed dictionary).

## 🔀 Low-code orchestration (Make.com)

The primary way to run this on a schedule is now
[Make.com](https://www.make.com), with Python doing only the parts that
are easier to get right in real code:

```
Make Scheduler
     │
     ├─ fetch Canvas ICS ──┐
     └─ fetch TimeEdit ICS ┘
                 │
                 ↓
     POST → Python API (src/api.py, on Render)
              parses the ICS quirks, diffs against
              Google Calendar's current state
                 │
                 ↓
     normalized create/update/delete actions
                 │
                 ↓
     Make executes them against Google Calendar
                 │
                 ↓
     (optional) Discord / Email notification
```

Setup:
1. Deploy `src/api.py`: Render dashboard -> New -> Blueprint -> point at
   this repo (`render.yaml` is already set up) -> fill in the
   `GOOGLE_TOKEN_JSON`, `GOOGLE_CALENDAR_ID`, and `API_KEY` secrets it
   prompts for (`GOOGLE_TOKEN_JSON` is the same value as the GitHub
   Actions secret below; generate `API_KEY` with
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
2. Build the Make scenario: see **[docs/make-scenario.md](docs/make-scenario.md)**
   for the full module-by-module walkthrough.

Why split it this way: Make owns scheduling, HTTP fetching, the actual
Google Calendar writes, and notifications -- all visible/editable in
Make's UI. Python keeps the part that's genuinely fiddly to get right
visually: matching events across runs so re-syncing never creates
duplicates (see `plan_sync` in `src/calendar_sync.py`).

## 🤖 Automation

The plain CLI (`python -m src.main`) still works standalone, and
`.github/workflows/sync.yml` still exists to run it -- but with Make as
the primary scheduler above, its `schedule:` trigger is commented out by
default. It's kept as a manual/backup path: trigger it by hand from the
Actions tab (`workflow_dispatch`), or uncomment the schedule if Make has
an outage or you exhaust its free-tier operations quota.

To use it (standalone or as the backup), go to **Settings -> Secrets and
variables -> Actions** and add these repo secrets:

- `CANVAS_ICS_URL` -- the Canvas ICS URL from step 1
- `TIMEEDIT_ICS_URL` -- the TimeEdit ICS URL from step 2
- `GOOGLE_CALENDAR_ID` -- the same value you put in `.env`
- `GOOGLE_TOKEN_JSON` -- the full contents of the `token.json` that
  `src/auth_setup.py` produced locally


The refresh token in `token.json` doesn't expire from scheduled use, only
from long inactivity (~6 months) or if you revoke it yourself
(myaccount.google.com/permissions). If that happens, just re-run
`src/auth_setup.py` locally and update the secret (and the `GOOGLE_TOKEN_JSON`
env var on Render, if you're running the Make architecture above).

