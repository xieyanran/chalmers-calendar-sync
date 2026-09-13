from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Event:
    source: str  # "canvas" | "timeedit" | "ladok"
    source_uid: str  # stable id within the source, used for dedupe
    category: str  # "assignment" | "class" | "exam" | "registration"
    title: str
    start: datetime
    end: datetime
    location: str = ""
    description: str = ""
    # True for deadlines/events with no real time-of-day (e.g. Canvas
    # assignments with a DATE-only due date, no DTEND). start/end are still
    # tz-aware datetimes at UTC midnight for uniform sorting; calendar_sync
    # renders these as Google Calendar all-day events instead of a
    # timestamp.
    all_day: bool = False

    @property
    def uid(self) -> str:
        return f"{self.source}-{self.source_uid}"


def stable_hash(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


# Best-effort, deterministic Swedish -> English mapping for the vocabulary
# TimeEdit actually uses. Word-boundary substring replace, not a translator:
# good enough for the fixed set of scheduling terms, not for free text.
# The compound-word entries (Gästföreläsning etc.) need their own keys --
# "Föreläsning" doesn't match inside them since there's no word boundary
# before the "F".
DEFAULT_SV_EN = {
    "Föreläsning": "Lecture",
    "Gästföreläsning": "Guest lecture",
    "Distansföreläsning": "Remote lecture",
    "Videoföreläsning": "Video lecture",
    "Konsultationstid": "Consultation hours",
    "Datorlaboration": "Computer lab",
    "Övning": "Exercise",
    "Laboration": "Lab",
    "Seminarium": "Seminar",
    "Tentamen": "Exam",
    "Omtentamen": "Re-exam",
    "Centralt administrerad dugga": "Centrally administered quiz",
    "Dugga": "Quiz",
    "Sal": "Room",
    "Grupp": "Group",
    "Lektion": "Lesson",
    "Kurs": "Course",
    "Introduktion": "Introduction",
    "Redovisning": "Presentation",
    "Handledning": "Supervision",
}


def translate_sv_en(text: str, extra: dict[str, str] | None = None) -> str:
    if not text:
        return text
    mapping = {**DEFAULT_SV_EN, **(extra or {})}
    for sv, en in mapping.items():
        text = re.sub(rf"\b{re.escape(sv)}\b", en, text)
    return text
