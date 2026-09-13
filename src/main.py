from __future__ import annotations

import argparse
import logging
import os
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

from src.calendar_sync import get_service, sync_events
from src.sources import canvas, timeedit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()  # no-op if .env doesn't exist (e.g. in GitHub Actions, which sets real env vars)

# Events are stored/synced in UTC (Google Calendar converts for the viewer
# regardless); this is only so --dry-run prints times you can sanity-check
# against the actual Chalmers schedule instead of raw UTC.
DISPLAY_TZ = ZoneInfo("Europe/Stockholm")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync Canvas/TimeEdit(/Ladok) into Google Calendar"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print events without writing to Google Calendar",
    )
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    events = []

    canvas_url = os.environ.get(config["canvas"]["ics_url_env"])
    if canvas_url:
        canvas_events = canvas.fetch(canvas_url)
        logger.info("Fetched %d events from Canvas", len(canvas_events))
        events += canvas_events
    else:
        logger.warning("Skipping Canvas: %s is not set", config["canvas"]["ics_url_env"])

    timeedit_url = os.environ.get(config["timeedit"]["ics_url_env"])
    if timeedit_url:
        timeedit_events = timeedit.fetch(
            timeedit_url,
            config["timeedit"].get("translations"),
            config["timeedit"].get("course_names"),
        )
        logger.info("Fetched %d events from TimeEdit", len(timeedit_events))
        events += timeedit_events
    else:
        logger.warning("Skipping TimeEdit: %s is not set", config["timeedit"]["ics_url_env"])

    # Ladok deliberately left out -- see src/sources/ladok.py's docstring.

    if args.dry_run:
        for event in sorted(events, key=lambda e: e.start):
            when = (
                f"{event.start.date():%Y-%m-%d} (all-day)"
                if event.all_day
                else f"{event.start.astimezone(DISPLAY_TZ):%Y-%m-%d %H:%M}"
            )
            print(f"[{event.source}/{event.category}] {when} {event.title} @ {event.location}")
        print(f"\n{len(events)} events total (dry run -- nothing written)")
        return

    token_file = os.environ.get(
        "GOOGLE_TOKEN_FILE",
        config["google_calendar"].get("token_file", "token.json"),
    )
    calendar_id = os.environ.get(
        config["google_calendar"].get("calendar_id_env", "GOOGLE_CALENDAR_ID"),
        config["google_calendar"].get("calendar_id"),
    )
    if not calendar_id:
        raise SystemExit(
            "No calendar ID set -- put it in the GOOGLE_CALENDAR_ID env var "
            "(see .env.example) or config.yaml's google_calendar.calendar_id."
        )

    service = get_service(token_file)
    stats = sync_events(
        service,
        calendar_id,
        events,
        config["reminders_minutes_before"],
    )
    logger.info("Sync complete: %s", stats)


if __name__ == "__main__":
    main()
