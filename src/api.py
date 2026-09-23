"""calendar-normalizer: the HTTP API a low-code orchestrator (Make.com) calls
into. Fetching (Canvas/TimeEdit) and the actual Google Calendar writes live
in the Make scenario -- see docs/make-scenario.md; this only does the part
that's easier to get right in real code: parsing Chalmers' ICS quirks and
diffing against Google Calendar's current state.

Run locally with:
    uvicorn src.api:app --reload
"""

from __future__ import annotations

import dataclasses
import hmac
import logging
import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from src.calendar_sync import get_service, plan_sync
from src.main import load_config
from src.sources import canvas, timeedit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

load_dotenv()  # no-op if .env doesn't exist (e.g. on Render, which sets real env vars)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.yaml")

app = FastAPI(
    title="calendar-normalizer",
    description="Normalizes Canvas/TimeEdit ICS feeds and diffs them "
    "against Google Calendar. Business logic only -- scheduling, fetching "
    "the feeds, and writing the resulting actions to Google Calendar are "
    "the caller's job (see docs/make-scenario.md).",
)


class PlanRequest(BaseModel):
    canvas_ics: str | None = None
    timeedit_ics: str | None = None


class PlanAction(BaseModel):
    kind: str
    title: str
    method: str
    url: str
    event_id: str | None = None
    body: dict | None = None


class PlanResponse(BaseModel):
    actions: list[PlanAction]
    stats: dict[str, int]
    event_count: int


def _require_api_key(x_api_key: str = Header(default="")) -> None:
    expected = os.environ.get("API_KEY", "")
    if not expected:
        # Fails closed: an unconfigured server refuses rather than silently
        # accepting any caller.
        raise HTTPException(500, "API_KEY is not configured on the server")
    if not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(401, "invalid or missing X-API-Key header")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/plan", response_model=PlanResponse, dependencies=[Depends(_require_api_key)])
def plan(request: PlanRequest) -> PlanResponse:
    if not request.canvas_ics and not request.timeedit_ics:
        # An empty plan would diff as "every existing managed event
        # disappeared" -- i.e. delete everything. That's never what an
        # empty request actually means, so refuse it outright rather than
        # let a caller-side mistake turn into a wiped calendar.
        raise HTTPException(400, "at least one of canvas_ics/timeedit_ics is required")

    config = load_config(CONFIG_PATH)
    events = []

    if request.canvas_ics:
        canvas_events = canvas.parse(request.canvas_ics)
        logger.info("Parsed %d events from Canvas", len(canvas_events))
        events += canvas_events

    if request.timeedit_ics:
        timeedit_events = timeedit.parse(
            request.timeedit_ics,
            config["timeedit"].get("translations"),
            config["timeedit"].get("course_names"),
        )
        logger.info("Parsed %d events from TimeEdit", len(timeedit_events))
        events += timeedit_events

    calendar_id = os.environ.get(
        config["google_calendar"].get("calendar_id_env", "GOOGLE_CALENDAR_ID"),
        config["google_calendar"].get("calendar_id"),
    )
    if not calendar_id:
        raise HTTPException(500, "No calendar ID configured (GOOGLE_CALENDAR_ID)")

    token_file = os.environ.get(
        "GOOGLE_TOKEN_FILE",
        config["google_calendar"].get("token_file", "token.json"),
    )
    service = get_service(token_file)
    actions, stats = plan_sync(service, calendar_id, events, config["reminders_minutes_before"])
    logger.info("Computed plan: %s", stats)

    return PlanResponse(
        actions=[PlanAction(**dataclasses.asdict(a)) for a in actions],
        stats=stats,
        event_count=len(events),
    )
