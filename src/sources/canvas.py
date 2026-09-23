from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import requests
from icalendar import Calendar

from src.normalize import Event


def fetch(ics_url: str | None = None) -> list[Event]:
    """Canvas exposes a per-user private ICS feed (Calendar page -> "Calendar
    Feed" link) covering assignment due dates and calendar events. Using it
    avoids needing a Personal Access Token for the common case."""
    url = ics_url or os.environ["CANVAS_ICS_URL"]
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return parse(response.content)  # see timeedit.py: avoid requests' charset guessing


def parse(ics_data: bytes | str) -> list[Event]:
    """Same as fetch(), but from ICS content someone else already fetched --
    e.g. a Make.com scenario that fetches the feed itself and POSTs the
    contents to src/api.py."""
    if isinstance(ics_data, str):
        ics_data = ics_data.encode("utf-8")
    calendar = Calendar.from_ical(ics_data)

    events: list[Event] = []
    for component in calendar.walk("VEVENT"):
        uid = str(component.get("UID"))
        if not uid.startswith("event-assignment-"):
            # Canvas's feed also includes plain calendar events (lecture
            # announcements, consultation-hour reminders, etc, prefixed
            # "event-calendar-event-"). TimeEdit already covers the class
            # schedule, so keep only actual Assignments here to match
            # "Canvas = deadlines".
            continue
        summary = str(component.get("SUMMARY", ""))
        dtstart_raw = component["DTSTART"].dt
        # Assignments with a plain due-date and no due-time (common for
        # project milestones) come through as `date`, with no DTEND at all --
        # render those as all-day events rather than a misleading midnight
        # timestamp.
        all_day = not isinstance(dtstart_raw, datetime)
        dtend = component.get("DTEND")
        if all_day:
            end_raw = dtend.dt if dtend else dtstart_raw + timedelta(days=1)
        else:
            end_raw = dtend.dt if dtend else dtstart_raw
        start = _to_datetime(dtstart_raw)
        end = _to_datetime(end_raw)
        description = str(component.get("DESCRIPTION", ""))
        location = str(component.get("LOCATION", ""))

        events.append(
            Event(
                source="canvas",
                source_uid=uid,
                category="assignment",
                title=summary,
                start=start,
                end=end,
                location=location,
                description=description,
                all_day=all_day,
            )
        )
    return events


def _to_datetime(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    # All-day entries come through as `date`, not `datetime`.
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
