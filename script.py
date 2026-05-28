"""
Pokémon GO event syncer for Toronto.
Fetches upcoming events from ScrapedDuck (LeekDuck mirror) and adds them
to a dedicated Google Calendar. Idempotent — re-running won't duplicate events.
"""

import os
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pogo-sync")

# --- config ---
# Primary source: ScrapedDuck (scrapes LeekDuck.com)
# Data lives on the 'data' branch of bigfoott/ScrapedDuck
SCRAPEDDUCK_URL = "https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/events.json"

LOCAL_TZ = ZoneInfo("America/Toronto")
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# Event types that are reliably global — happen worldwide at the same time
# (either same local time everywhere, or same UTC moment).
GLOBAL_EVENT_TYPES = {
    "community-day",
    "spotlight-hour",
    "raid-hour",
    "raid-day",
    "raid-battles",
    "elite-raids",
    "research",
    "research-day",
    "season",
    "event",
    "ticketed-event",
}

# Event types that are usually in-person/location-specific.
# We skip these unless the name explicitly says "global".
LOCATION_SPECIFIC_TYPES = {
    "live-event",
    "safari-zone",
    "go-tour",
    "city-safari",
    "pokemon-go-fest",  # GO Fest has both global and city versions
}

# Words in event names that strongly suggest an in-person/regional event,
# even if the type looks global. Used as a secondary filter.
LOCATION_SPECIFIC_NAME_HINTS = {
    "fossil museum",
    "pop-up",
    "in-person",
    "ticketed",
    "city safari",
}

# Words that confirm an event is for everyone, even if its type is normally local.
GLOBAL_NAME_HINTS = {
    "global",
    "worldwide",
}


def fetch_events():
    """Fetch events from ScrapedDuck."""
    log.info("Fetching events from %s", SCRAPEDDUCK_URL)
    r = requests.get(SCRAPEDDUCK_URL, timeout=30)
    r.raise_for_status()
    data = r.json()
    log.info("Got %d events", len(data))
    return data


def get_calendar_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def parse_event_time(event, key):
    """ScrapedDuck times are ISO 8601 strings. Some are UTC ('Z'), some are local-naive."""
    raw = event.get(key)
    if not raw:
        return None
    # Replace trailing Z with +00:00 for fromisoformat
    raw = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        log.warning("Could not parse time %r", raw)
        return None
    # If naive, ScrapedDuck convention for community-day type events is local time
    # (the event runs at the same local time everywhere). Treat as Toronto local.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


def make_event_id(event):
    """Stable ID per event so reruns update rather than duplicate.
    Google requires IDs to be lowercase letters/digits and 5-1024 chars."""
    base = f"{event.get('eventID') or event.get('name', '')}-{event.get('start', '')}"
    # keep only base32-safe chars
    safe = "".join(c for c in base.lower() if c.isalnum())
    # pad if too short
    if len(safe) < 5:
        safe = (safe + "pogosync")[:10]
    return safe[:1024]


def build_calendar_event(event):
    name = event.get("name", "Pokémon GO event")
    heading = event.get("heading", "")
    event_type = event.get("eventType", "")
    link = event.get("link", "")
    image = event.get("image", "")

    start = parse_event_time(event, "start")
    end = parse_event_time(event, "end")
    if not start or not end:
        return None

    # Build a readable title
    if heading and heading.lower() not in name.lower():
        title = f"{heading}: {name}"
    else:
        title = name

    description_lines = []
    if event_type:
        description_lines.append(f"Type: {event_type}")
    if link:
        description_lines.append(f"More info: {link}")
    if image:
        description_lines.append(f"Image: {image}")
    description_lines.append("\nAuto-added by pokemon-go-calendar sync.")

    return {
        "id": make_event_id(event),
        "summary": title,
        "description": "\n".join(description_lines),
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Toronto"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Toronto"},
        "source": {"title": "LeekDuck", "url": link} if link else None,
    }


def name_suggests_global(event):
    name = (event.get("name") or "").lower()
    heading = (event.get("heading") or "").lower()
    text = f"{name} {heading}"
    return any(hint in text for hint in GLOBAL_NAME_HINTS)


def name_suggests_local(event):
    name = (event.get("name") or "").lower()
    heading = (event.get("heading") or "").lower()
    text = f"{name} {heading}"
    return any(hint in text for hint in LOCATION_SPECIFIC_NAME_HINTS)


def should_include(event):
    event_type = (event.get("eventType") or "").lower()
    heading = (event.get("heading") or "").lower()

    # If the name explicitly says "global", include it regardless of type.
    if name_suggests_global(event):
        return True

    # If the name hints at being in-person/location-specific, skip it.
    if name_suggests_local(event):
        return False

    # Location-specific types only get included if they said "global" above.
    if event_type in LOCATION_SPECIFIC_TYPES:
        return False
    if any(t in heading for t in LOCATION_SPECIFIC_TYPES):
        return False

    # Otherwise include if it's a known global type.
    if event_type in GLOBAL_EVENT_TYPES:
        return True
    if any(t in heading for t in GLOBAL_EVENT_TYPES):
        return True

    # Unknown type — default to skip. Better to miss an event than spam the calendar.
    return False


def is_future(event):
    """Only sync events that haven't ended yet."""
    end = parse_event_time(event, "end")
    if not end:
        return False
    return end > datetime.now(timezone.utc)


def sync():
    calendar_id = os.environ["GOOGLE_CALENDAR_ID"].strip()
    service = get_calendar_service()

    events = fetch_events()
    filtered = [e for e in events if should_include(e) and is_future(e)]
    log.info("After filtering: %d events to sync", len(filtered))

    created, updated, skipped = 0, 0, 0
    for raw in filtered:
        cal_event = build_calendar_event(raw)
        if not cal_event:
            skipped += 1
            continue
        # strip None values that the API won't accept
        cal_event = {k: v for k, v in cal_event.items() if v is not None}
        event_id = cal_event["id"]
        try:
            # Try to update first; if not found, insert.
            service.events().update(
                calendarId=calendar_id, eventId=event_id, body=cal_event
            ).execute()
            updated += 1
            log.info("Updated: %s", cal_event["summary"])
        except Exception as e:
            if "404" in str(e) or "Not Found" in str(e):
                try:
                    service.events().insert(
                        calendarId=calendar_id, body=cal_event
                    ).execute()
                    created += 1
                    log.info("Created: %s", cal_event["summary"])
                except Exception as e2:
                    log.error("Failed to create %s: %s", cal_event["summary"], e2)
                    skipped += 1
            else:
                log.error("Failed to update %s: %s", cal_event["summary"], e)
                skipped += 1

    log.info("Done. Created=%d Updated=%d Skipped=%d", created, updated, skipped)


if __name__ == "__main__":
    sync()
