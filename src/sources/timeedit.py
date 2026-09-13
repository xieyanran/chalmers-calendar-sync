from __future__ import annotations

import os
import re
from datetime import date, datetime, timezone

import requests
from icalendar import Calendar

from src.normalize import Event, translate_sv_en

# Chalmers' TimeEdit packs cross-listed course codes, an optional course
# name (repeated once per code), the activity type, and programme/cohort
# tags into one comma-separated SUMMARY. Two template variants observed so
# far:
#   "Course code: DIT969GU. Name: Djup maskininlärning, Course code:
#    SSY340_50_HT26_35125. Name: Djup maskininlärning, Föreläsning,
#    MPCAS-2, MPALG-2, MPCSN-2, MPSYS-2, MPMED-2, MPDSC-2"
#   "Kurskod: DIT969GU, Kurskod: SSY340_50_HT26_35125, Aktivitet:
#    Konsultationstid, Klasskod: MPCAS-2, Klasskod: MPALG-2"
# i.e. the course name and the "Aktivitet"/programme-tag labels are
# sometimes present and sometimes not, and labels flip between Swedish and
# English -- observed flipping between two runs of the *same* feed URL
# minutes apart, so this has to accept every variant rather than assume any
# one is stable.
_COURSE_RE = re.compile(
    r"^(?:Course code|Kurskod):\s*(?P<code>.+?)"
    r"(?:\.\s*(?:(?:Name|Namn):\s*)?(?P<name>.+))?$"
)
_ACTIVITY_LABELS = {"Activity", "Aktivitet"}
# Programme/cohort tags like "MPCAS-2" or "Klasskod: MPCAS-2" -- not useful
# on a personal calendar.
_PROGRAMME_LABELS = {"Class code", "Klasskod"}
_BARE_PROGRAMME_RE = re.compile(r"^[A-ZÅÄÖ]{2,8}-\d+$")

# LOCATION is similarly a ". "-joined bag of "Key: value" pairs (Rum,
# Lokalnamn, Utrustning, Kartlänk, Hus, Campus, Antal datorer, ...) plus bare
# continuation words for multi-value keys (e.g. equipment lists). Only these
# three are worth keeping on a calendar. Keyed bilingually for the same
# reason as _COURSE_RE above -- not confirmed Swedish-only, just under-sampled.
_LOCATION_KEYS = {
    "Rum": "room",
    "Room": "room",
    "Lokalnamn": "room",
    "Hus": "building",
    "Building": "building",
    "Campus": "campus",
}


def fetch(
    ics_url: str | None = None,
    translations: dict[str, str] | None = None,
    course_names: dict[str, str] | None = None,
) -> list[Event]:
    """Fetch TimeEdit's ICS *subscription* URL (not a one-off manual export --
    it must be fetchable with a plain HTTP GET, no login). Look for a
    "Subscribe"/"Prenumerera" link in the TimeEdit UI, not the download
    button.

    `course_names` is a short-course-code -> display-name fallback (see
    config.yaml) for feed variants whose SUMMARY omits the Name field
    entirely."""
    url = ics_url or os.environ["TIMEEDIT_ICS_URL"]
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    # Pass raw bytes, not response.text: iCal is UTF-8 per RFC 5545, but
    # `requests` guesses a charset from headers and gets it wrong when the
    # server omits one, mangling the Swedish characters. icalendar decodes
    # bytes correctly on its own.
    calendar = Calendar.from_ical(response.content)

    events: list[Event] = []
    for component in calendar.walk("VEVENT"):
        uid = str(component.get("UID"))
        raw_summary = str(component.get("SUMMARY", ""))
        raw_location = str(component.get("LOCATION", ""))
        start = _to_datetime(component["DTSTART"].dt)
        dtend = component.get("DTEND")
        end = _to_datetime(dtend.dt) if dtend else start

        events.append(
            Event(
                source="timeedit",
                source_uid=uid,
                category="class",
                title=_parse_summary(raw_summary, translations, course_names),
                start=start,
                end=end,
                location=_parse_location(raw_location),
            )
        )
    return events


def _parse_summary(
    summary: str,
    translations: dict[str, str] | None,
    course_names: dict[str, str] | None = None,
) -> str:
    codes: list[str] = []
    feed_names: dict[str, str] = {}
    activities: list[str] = []

    for part in summary.split(", "):
        part = part.strip()
        if not part:
            continue
        course_match = _COURSE_RE.match(part)
        if course_match:
            code = _short_course_code(course_match.group("code"))
            name = (course_match.group("name") or "").strip()
            if code not in codes:
                codes.append(code)
            if name and code not in feed_names:
                feed_names[code] = name
            continue
        if _BARE_PROGRAMME_RE.match(part):
            continue  # cohort/programme tag, e.g. "MPCAS-2"
        label, _, value = part.partition(": ")
        if label in _PROGRAMME_LABELS:
            continue  # cohort/programme tag, e.g. "Klasskod: MPCAS-2"
        if label in _ACTIVITY_LABELS:
            activities.append(translate_sv_en(value.strip(), translations))
        else:
            activities.append(translate_sv_en(part, translations))

    if not codes:
        # Unrecognized layout (a template variant not seen before) -- don't
        # fabricate structure from it, just translate what we can. Some
        # entries (e.g. "Omtentamen/Re-exam") are already bilingual, so
        # translating "Omtentamen" would otherwise duplicate the English
        # half into "Re-exam/Re-exam" -- collapse that back down.
        return _dedupe_slash(translate_sv_en(summary, translations))

    # config.yaml's course_names always wins over whatever the feed itself
    # sends as the Name/Namn field: TimeEdit's SUMMARY layout has been
    # observed changing shape between runs of the very same feed URL (see
    # module docstring), so pinning the display name in config is what
    # keeps titles stable across those flips instead of just papering over
    # the one variant seen so far.
    names: list[str] = []
    for code in codes:
        name = (course_names or {}).get(code) or feed_names.get(code)
        if name and name not in names:
            names.append(name)

    if names:
        course_label = translate_sv_en(" / ".join(names), translations)
        course_label += f" [{'/'.join(codes)}]"
    else:
        course_label = "/".join(codes)
    if activities:
        return f"{' / '.join(activities)} - {course_label}"
    return course_label


def _short_course_code(code: str) -> str:
    return code.split("_")[0]


def _dedupe_slash(text: str) -> str:
    parts = [p.strip() for p in text.split("/")]
    deduped = list(dict.fromkeys(parts))
    return "/".join(deduped)


def _parse_location(location: str) -> str:
    values: dict[str, str] = {}
    for part in location.split(". "):
        key, sep, value = part.partition(": ")
        if sep and key in _LOCATION_KEYS:
            values[_LOCATION_KEYS[key]] = value.strip()
    return ", ".join(values[k] for k in ("room", "building", "campus") if k in values)


def _to_datetime(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
