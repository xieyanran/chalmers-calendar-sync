# Building the Make.com scenario

This is the low-code half of the architecture: Make owns scheduling,
fetching the two ICS feeds, writing to Google Calendar, and (later)
notifications. Python (`src/api.py`, deployed on Render) only does the part
that's easier to get right in real code: parsing Chalmers' ICS quirks and
diffing against Google Calendar's current state. See the repo README for
the high-level picture; this doc is the module-by-module build steps.

I can't create the scenario for you (no Make API/MCP access from here) --
this is a build guide, not an importable blueprint. Exact field labels
below may drift slightly from what you see in the Make UI (it changes
over time); the module types and data flow won't.

## Prerequisites

- `src/api.py` deployed on Render (see `render.yaml` / the README), with
  its URL (`https://your-service.onrender.com`) and the `API_KEY` you set
  there.
- Your Canvas and TimeEdit ICS URLs (same ones from `.env`).
- A Make.com account, with its own Google Calendar connection (Make ->
  Connections -> Add -> Google Calendar -> log in as the same account that
  owns the target calendar). This is separate from `token.json` -- Python
  and Make each hold their own Google credential.

## Scenario modules

1. **Scheduler** -- e.g. every 4 hours, matching the old GitHub Actions
   cadence. Free-tier Make bills in "operations" (roughly: one per module
   execution), so check this run frequency against your plan's monthly
   quota once notifications are added later.

2. **HTTP > Make a request** -- `GET` your Canvas ICS URL.

3. **HTTP > Make a request** -- `GET` your TimeEdit ICS URL. (Modules 2 and
   3 don't depend on each other -- Make runs them in the same sequence but
   nothing here needs a Router to parallelize; keep it simple.)

4. **HTTP > Make a request** -- `POST {RENDER_URL}/plan`:
   - Headers: `X-API-Key: <your API_KEY>`, `Content-Type: application/json`
   - Body type: raw / JSON
   - Body:
     ```json
     {
       "canvas_ics": "{{2.data}}",
       "timeedit_ics": "{{3.data}}"
     }
     ```
     (map from whichever text/body output field modules 2 and 3 expose --
     Make usually calls it `Data`; use the response body, not `Headers` or
     `Status code`.)
   - Response shape (see `src/api.py`):
     ```json
     {
       "actions": [
         {"kind": "create", "title": "...", "method": "POST", "url": "https://www.googleapis.com/calendar/v3/calendars/.../events", "event_id": null, "body": {...}},
         {"kind": "update", "title": "...", "method": "PATCH", "url": ".../events/abc123", "event_id": "abc123", "body": {...}},
         {"kind": "delete", "title": "...", "method": "DELETE", "url": ".../events/abc123", "event_id": "abc123", "body": null}
       ],
       "stats": {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0},
       "event_count": 74
     }
     ```
   - **Cold start**: Render's free tier sleeps after 15 min idle and can
     take up to ~60s to wake up, which may exceed this module's default
     timeout on the first request of a run. Add an error handler on this
     module (right-click -> "Add error handler") using **Resume** or
     **Break** with a ~60-90s delay before retrying once, rather than
     raising the scenario's run frequency or the timeout indefinitely.

5. **Iterator** -- over `{{4.data.actions}}`.

6. **Router**, with 3 routes filtered on the iterator's `kind` field:
   - Route A: `kind = create`
   - Route B: `kind = update`
   - Route C: `kind = delete`

7. **Per route: Google Calendar > "Make an API call"** (listed under
   Google Calendar's "Other" actions in Make) -- this sends the exact JSON
   `body` Python computed straight through, using Make's own Google
   Calendar connection for auth. This matters, not just convenience: it's
   what keeps `extendedProperties.private.managedBy`/`sourceUid` intact on
   every event Make creates. If you instead use Google Calendar's plain
   *Create an Event* / *Update an Event* UI modules, double-check they
   expose an `extendedProperties` field before relying on them -- if they
   don't, next run's `/plan` diff won't recognize those events as
   already-managed and will plan to recreate them every single time.
   - Route A (create): Method `POST`, URL `{{6.url}}`, Body `{{6.body}}`
   - Route B (update): Method `PATCH`, URL `{{6.url}}`, Body `{{6.body}}`
   - Route C (delete): Method `DELETE`, URL `{{6.url}}`, no body
   - (Module numbers above are illustrative -- map from whatever the
     Iterator's actual module number is in your scenario.)

## Adding notifications later

`{{4.data.stats}}` (`created`/`updated`/`deleted`/`unchanged` counts) is
already in the `/plan` response, computed before anything is applied --
so wiring up a Discord webhook or Make's Email module is a Make-only
change whenever you want it: add a module after the Router finishes,
formatting a message from those four numbers. No Python changes needed.

## Testing

Before trusting the schedule, run the scenario once manually (Make's "Run
once" button) and check:
- The `/plan` call returns the same event/action counts as
  `python -m src.main --dry-run` for the same feeds.
- A handful of created events actually show up on the calendar, and
  re-running the scenario immediately after reports them as `unchanged`
  (not recreated) -- this is the real test of the `extendedProperties`
  requirement above.
- Swedish characters (å/ä/ö) in TimeEdit titles/locations aren't mangled --
  Chalmers' TimeEdit feed does contain them, even though most end up
  translated away by `config.yaml`'s `timeedit.translations`/`course_names`.
