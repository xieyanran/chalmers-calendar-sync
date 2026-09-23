from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable
from urllib.parse import quote

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from src.normalize import Event

logger = logging.getLogger(__name__)

# Chalmers' Google Workspace org blocks service-account key creation
# (iam.disableServiceAccountKeyCreation), so this authenticates as the user
# directly via OAuth instead -- see src/auth_setup.py for the one-time login
# that produces token.json. Bonus: since it's the user's own calendar under
# their own account, there's no separate "share the calendar" step needed.
SCOPES = ["https://www.googleapis.com/auth/calendar"]
MANAGED_BY_KEY = "managedBy"
MANAGED_BY_VALUE = "chalmers-calendar-sync"
SOURCE_UID_KEY = "sourceUid"

_EVENTS_API_BASE = "https://www.googleapis.com/calendar/v3/calendars"


def get_service(token_file: str):
    # On a long-lived host (e.g. the Render deploy backing src/api.py) we'd
    # rather never write the refresh token to disk -- GOOGLE_TOKEN_JSON (the
    # same value the GOOGLE_TOKEN_JSON GitHub Actions secret holds) lets the
    # credentials be built straight from the env instead.
    token_json = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_json:
        credentials = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    else:
        try:
            credentials = Credentials.from_authorized_user_file(token_file, SCOPES)
        except FileNotFoundError:
            raise SystemExit(
                f"{token_file} not found. Run `python -m src.auth_setup` once locally "
                "to log in and create it (see README)."
            )
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


@dataclass
class Action:
    """One pending Google Calendar write, computed by plan_sync(). `url`/
    `method` are the real Calendar REST endpoint for this action -- so a
    caller with its own Google OAuth connection (e.g. a Make.com HTTP
    module) can execute it directly, without re-deriving anything."""

    kind: str  # "create" | "update" | "delete"
    title: str
    method: str  # "POST" | "PATCH" | "DELETE"
    url: str
    event_id: str | None = None  # set for update/delete
    body: dict | None = None  # set for create/update


def _event_url(calendar_id: str, event_id: str | None = None) -> str:
    base = f"{_EVENTS_API_BASE}/{quote(calendar_id, safe='')}/events"
    return f"{base}/{quote(event_id, safe='')}" if event_id else base


def _to_google_event(event: Event, reminders_minutes: list[int]) -> dict:
    if event.all_day:
        start_body = {"date": event.start.date().isoformat()}
        end_body = {"date": event.end.date().isoformat()}
    else:
        start_body = {"dateTime": event.start.isoformat()}
        end_body = {"dateTime": event.end.isoformat()}

    body = {
        "summary": event.title,
        "location": event.location,
        "description": event.description,
        "start": start_body,
        "end": end_body,
        "extendedProperties": {
            "private": {
                MANAGED_BY_KEY: MANAGED_BY_VALUE,
                SOURCE_UID_KEY: event.uid,
            }
        },
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": m} for m in reminders_minutes],
        },
    }
    return body


def _list_managed_events(service, calendar_id: str) -> dict[str, dict]:
    """Return {source_uid: google_event} for every event this tool manages."""
    existing: dict[str, dict] = {}
    page_token = None
    while True:
        response = (
            service.events()
            .list(
                calendarId=calendar_id,
                privateExtendedProperty=f"{MANAGED_BY_KEY}={MANAGED_BY_VALUE}",
                pageToken=page_token,
                showDeleted=False,
                maxResults=2500,
            )
            .execute()
        )
        for item in response.get("items", []):
            source_uid = (
                item.get("extendedProperties", {}).get("private", {}).get(SOURCE_UID_KEY)
            )
            if source_uid:
                existing[source_uid] = item
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return existing


def _parsed_time(time_body: dict) -> datetime | date | None:
    """A Google Calendar start/end object has either `dateTime` (timed) or
    `date` (all-day) -- never both. Parse rather than compare raw strings:
    Google may echo a `dateTime` back with a different (but equal) UTC
    offset than what we sent -- e.g. the calendar's own timezone offset
    instead of "+00:00" -- which would otherwise look like a change on
    every single run forever."""
    if "dateTime" in time_body:
        return datetime.fromisoformat(time_body["dateTime"].replace("Z", "+00:00"))
    if "date" in time_body:
        return date.fromisoformat(time_body["date"])
    return None


def _needs_update(google_event: dict, new_body: dict) -> bool:
    for field_name in ("summary", "location", "description"):
        if google_event.get(field_name, "") != new_body.get(field_name, ""):
            return True
    if _parsed_time(google_event.get("start", {})) != _parsed_time(new_body["start"]):
        return True
    if _parsed_time(google_event.get("end", {})) != _parsed_time(new_body["end"]):
        return True
    return False


def plan_sync(
    service,
    calendar_id: str,
    events: Iterable[Event],
    reminders_by_category: dict[str, list[int]],
) -> tuple[list[Action], dict[str, int]]:
    """Diff `events` against Google Calendar's current state and return what
    would need to change, without writing anything. Matched via the
    sourceUid stashed in extendedProperties.private, so this is idempotent:
    re-applying the same plan twice never creates duplicates. `stats` is
    derived from the diff itself, so it's accurate even if the caller never
    applies the plan (e.g. --dry-run, or a Make.com scenario that executes
    the actions itself)."""
    events = list(events)
    existing = _list_managed_events(service, calendar_id)
    seen_uids: set[str] = set()
    actions: list[Action] = []
    stats = {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0}

    for event in events:
        seen_uids.add(event.uid)
        reminders = reminders_by_category.get(event.category, [])
        body = _to_google_event(event, reminders)

        if event.uid in existing:
            google_event = existing[event.uid]
            if _needs_update(google_event, body):
                actions.append(
                    Action(
                        kind="update",
                        title=event.title,
                        method="PATCH",
                        url=_event_url(calendar_id, google_event["id"]),
                        event_id=google_event["id"],
                        body=body,
                    )
                )
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1
        else:
            actions.append(
                Action(
                    kind="create",
                    title=event.title,
                    method="POST",
                    url=_event_url(calendar_id),
                    body=body,
                )
            )
            stats["created"] += 1

    for source_uid, google_event in existing.items():
        if source_uid not in seen_uids:
            actions.append(
                Action(
                    kind="delete",
                    title=google_event.get("summary", ""),
                    method="DELETE",
                    url=_event_url(calendar_id, google_event["id"]),
                    event_id=google_event["id"],
                )
            )
            stats["deleted"] += 1

    return actions, stats


def apply_actions(service, calendar_id: str, actions: Iterable[Action]) -> None:
    """Execute a plan from plan_sync() via the googleapiclient service
    (rather than the raw method/url on each Action, which is there for
    non-Python callers like a Make.com HTTP module)."""
    for action in actions:
        if action.kind == "create":
            service.events().insert(calendarId=calendar_id, body=action.body).execute()
            logger.info("Created: %s", action.title)
        elif action.kind == "update":
            service.events().patch(
                calendarId=calendar_id, eventId=action.event_id, body=action.body
            ).execute()
            logger.info("Updated: %s", action.title)
        elif action.kind == "delete":
            service.events().delete(calendarId=calendar_id, eventId=action.event_id).execute()
            logger.info("Deleted: %s", action.title)


def sync_events(
    service,
    calendar_id: str,
    events: Iterable[Event],
    reminders_by_category: dict[str, list[int]],
    dry_run: bool = False,
) -> dict[str, int]:
    """Idempotent upsert: insert new events, update changed ones, delete ones
    that disappeared from the source. Thin wrapper around plan_sync() +
    apply_actions() for the CLI path -- see src/api.py for the split-apart
    version a Make.com scenario drives instead."""
    actions, stats = plan_sync(service, calendar_id, events, reminders_by_category)
    if dry_run:
        for action in actions:
            logger.info("Would %s: %s", action.kind, action.title)
    else:
        apply_actions(service, calendar_id, actions)
    return stats
