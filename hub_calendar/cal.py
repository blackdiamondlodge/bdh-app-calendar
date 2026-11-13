"""Calendar logic: who is looking, feed refresh, and expanding events into a date range.

Times are handled as *local wall-clock* times in the hub's time zone
(Settings > Location, via ``bdh_sdk.location``); the database stores aware UTC datetimes.
"""
import hashlib
import logging
import threading
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from bdh_sdk.auth import is_admin
from bdh_sdk.location import location as hub_location
from bdh_sdk.settings import app_setting
from dateutil import rrule
from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections
from django.utils import timezone

from .models import Event, Feed

log = logging.getLogger(__name__)

APP = "hub_calendar"
GUEST_EVENT_TTL = timedelta(days=14)  # when the stay has no checkout time
MAX_ICS_BYTES = 8 * 1024 * 1024
FETCH_TIMEOUT = 15
REFRESH_LOCK = "hub_calendar:refreshing"


# ---- time ---------------------------------------------------------------

def site_tz():
    where = hub_location()
    name = (where.timezone if where else "") or settings.TIME_ZONE
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def now_local():
    return timezone.now().astimezone(site_tz()).replace(tzinfo=None)


def to_local(dt):
    """Aware datetime -> naive local wall-clock time."""
    return dt.astimezone(site_tz()).replace(tzinfo=None)


def from_local(naive):
    """Naive local wall-clock time -> aware datetime."""
    return naive.replace(tzinfo=site_tz())


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ---- who is looking ------------------------------------------------------

def _current_stay(request):
    try:
        from hub.modes import current_stay
    except ImportError:  # running outside the hub
        return None
    try:
        return current_stay(request)
    except Exception:
        log.exception("stay lookup failed")
        return None


def stay_key(stay):
    """Same id the hub exposes as ``hub_stay_key``."""
    arrival = stay["arrival"].isoformat() if stay.get("arrival") else ""
    return hashlib.sha256(f"{stay['guest']}|{arrival}".encode()).hexdigest()[:12]


class Viewer:
    """Who is using the calendar and what they may change.

    Guests (a stay is in progress and nobody is signed in) only change the
    events they added during their stay; those events are deleted at checkout.
    Everyone else changes any event added on the hub. Feed events are read-only.
    """

    def __init__(self, request):
        self.admin = is_admin(request)
        stay = None
        if not request.user.is_authenticated:
            stay = _current_stay(request)
            if stay and stay.get("owner"):
                stay = None
        self.stay = stay
        self.guest = stay is not None
        self.stay_key = stay_key(stay) if stay else ""

    def can_edit(self, event):
        if not self.guest:
            return True
        return bool(event.stay_key) and event.stay_key == self.stay_key

    def new_event_fields(self):
        """Ownership fields for an event created now."""
        if not self.guest:
            return {"stay_key": "", "expires_at": None}
        checkout = self.stay.get("checkout")
        if checkout is not None and timezone.is_naive(checkout):
            checkout = from_local(checkout)
        return {"stay_key": self.stay_key,
                "expires_at": checkout or timezone.now() + GUEST_EVENT_TTL}


def purge_expired():
    """Delete events guests added once their stay is over."""
    Event.objects.filter(expires_at__isnull=False, expires_at__lte=timezone.now()).delete()


# ---- feeds ----------------------------------------------------------------

def normalize_url(url):
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    return url


def parse_ics(text):
    import icalendar
    import x_wr_timezone

    cal = icalendar.Calendar.from_ical(text)
    return x_wr_timezone.to_standard(cal)


def fetch_ics(url):
    import requests

    r = requests.get(url, timeout=FETCH_TIMEOUT, stream=True,
                     headers={"User-Agent": "BlackDiamondHub-Calendar/1.0"})
    r.raise_for_status()
    chunks, size = [], 0
    for chunk in r.iter_content(65536):
        size += len(chunk)
        if size > MAX_ICS_BYTES:
            raise ValueError("calendar is larger than 8 MB")
        chunks.append(chunk)
    # iCal is UTF-8 by spec; requests would guess Latin-1 for text/calendar.
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in text[:4096].upper():
        raise ValueError("that link didn't return an iCal calendar")
    return text


def refresh_feed(feed):
    """Download a feed. On failure the last good copy is kept."""
    feed.last_attempt = timezone.now()
    try:
        text = fetch_ics(feed.url)
        parse_ics(text)
    except Exception as exc:
        feed.last_error = (str(exc) or exc.__class__.__name__)[:300]
        feed.save(update_fields=["last_attempt", "last_error"])
        log.warning("calendar feed %s failed: %s", feed.name, feed.last_error)
        return False
    feed.ics = text
    feed.fetched_at = feed.last_attempt
    feed.last_error = ""
    feed.save(update_fields=["ics", "fetched_at", "last_attempt", "last_error"])
    return True


def refresh_minutes():
    try:
        return max(5, int(app_setting(APP, "refresh", 15)))
    except (TypeError, ValueError):
        return 15


def stale_feeds():
    cutoff = timezone.now() - timedelta(minutes=refresh_minutes())
    return [f for f in Feed.objects.filter(enabled=True)
            if f.last_attempt is None or f.last_attempt < cutoff]


def _refresh_worker(ids):
    try:
        for feed in Feed.objects.filter(id__in=ids):
            refresh_feed(feed)
    except Exception:
        log.exception("calendar refresh failed")
    finally:
        cache.delete(REFRESH_LOCK)
        close_old_connections()


def kick_refresh():
    """Refresh stale feeds in the background; returns at once."""
    ids = [f.id for f in stale_feeds()]
    if not ids or not cache.add(REFRESH_LOCK, 1, 300):
        return False
    threading.Thread(target=_refresh_worker, args=(ids,), daemon=True,
                     name="hub-calendar-refresh").start()
    return True


_parsed = {}  # feed id -> (fetched_at, calendar)


def _feed_calendar(feed):
    hit = _parsed.get(feed.id)
    if hit and hit[0] == feed.fetched_at:
        return hit[1]
    try:
        cal = parse_ics(feed.ics)
    except Exception:
        log.exception("calendar feed %s cached copy is unreadable", feed.name)
        return None
    _parsed[feed.id] = (feed.fetched_at, cal)
    return cal


def _local(value, tz):
    """iCal DTSTART/DTEND value -> (naive local datetime, is_date)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(tz).replace(tzinfo=None)
        return value, False
    return datetime.combine(value, time()), True


def feed_occurrences(feed, start, end):
    """Occurrences of a feed overlapping [start, end) (naive local datetimes)."""
    import recurring_ical_events

    cal = _feed_calendar(feed) if feed.ics else None
    if cal is None:
        return []
    tz = site_tz()
    people = [p.id for p in feed.people.all()]
    out = []
    try:
        comps = recurring_ical_events.of(cal, skip_bad_series=True).between(
            start.date() - timedelta(days=1), end.date() + timedelta(days=1))
    except Exception:
        log.exception("calendar feed %s could not be expanded", feed.name)
        return []
    for comp in comps:
        if comp.name != "VEVENT" or "DTSTART" not in comp:
            continue
        s, all_day = _local(comp["DTSTART"].dt, tz)
        if "DTEND" in comp:
            e, _ = _local(comp["DTEND"].dt, tz)
        elif "DURATION" in comp:
            e = s + comp["DURATION"].dt
        else:
            e = s + (timedelta(days=1) if all_day else timedelta())
        if e < s:
            e = s
        if not (s < end and (e > start or (e == s and s >= start))):
            continue
        uid = str(comp.get("UID", "")) or f"{s}"
        out.append({
            "id": f"f{feed.id}:{uid}:{iso(s)}",
            "source": f"feed:{feed.id}",
            "title": str(comp.get("SUMMARY", "")) or "(no title)",
            "start": iso(s), "end": iso(e), "all_day": all_day,
            "location": str(comp.get("LOCATION", "")),
            "notes": str(comp.get("DESCRIPTION", ""))[:2000],
            "color": feed.color, "people": people,
            "editable": False, "repeat": "", "feed": feed.name,
        })
    return out


# ---- hub events -------------------------------------------------------------

_FREQ = {
    "daily": dict(freq=rrule.DAILY),
    "weekdays": dict(freq=rrule.DAILY, byweekday=(rrule.MO, rrule.TU, rrule.WE, rrule.TH, rrule.FR)),
    "weekly": dict(freq=rrule.WEEKLY),
    "biweekly": dict(freq=rrule.WEEKLY, interval=2),
    "monthly": dict(freq=rrule.MONTHLY),
    "yearly": dict(freq=rrule.YEARLY),
}


def wall_duration(event):
    """Length on the clock, so an all-day event stays a whole day across DST."""
    return to_local(event.end) - to_local(event.start)


def occurrence_starts(event, start, end):
    """Local start times of ``event`` whose occurrence overlaps [start, end).

    Repeats follow wall-clock time, so a 7 pm event stays at 7 pm across DST.
    """
    s = to_local(event.start)
    dur = wall_duration(event)
    if not event.repeat:
        return [s] if s < end and s + dur > start or (dur == timedelta() and start <= s < end) else []
    kw = dict(_FREQ[event.repeat])
    if event.until:
        kw["until"] = datetime.combine(event.until, time(23, 59, 59))
    rule = rrule.rrule(dtstart=s, **kw)
    skip = set(event.exdates or [])
    return [o for o in rule.between(start - dur, end, inc=True)
            if iso(o) not in skip and o < end and (o + dur > start or (dur == timedelta() and o >= start))]


def hub_occurrences(start, end, viewer, color):
    out = []
    events = (Event.objects.filter(start__lt=from_local(end))
              .prefetch_related("people"))
    for ev in events:
        if not ev.repeat and ev.end <= from_local(start) and ev.end != ev.start:
            continue
        people = [p.id for p in ev.people.all()]
        dur = wall_duration(ev)
        for o in occurrence_starts(ev, start, end):
            out.append({
                "id": f"h{ev.id}:{iso(o)}",
                "source": "hub", "event": ev.id, "occ": iso(o),
                "title": ev.title, "start": iso(o), "end": iso(o + dur),
                "all_day": ev.all_day, "location": ev.location, "notes": ev.notes,
                "color": color, "people": people,
                "editable": viewer.can_edit(ev), "guest": bool(ev.stay_key),
                "repeat": ev.repeat, "until": ev.until.isoformat() if ev.until else "",
                "series_start": iso(to_local(ev.start)),
            })
    return out


def hub_color():
    c = str(app_setting(APP, "hub_color", "#3fbf7f") or "").strip()
    return c if len(c) == 7 and c.startswith("#") else "#3fbf7f"


def occurrences(start, end, viewer, feeds=None):
    """All events overlapping [start, end), sorted by start."""
    feeds = list(Feed.objects.filter(enabled=True).prefetch_related("people")) if feeds is None else feeds
    out = hub_occurrences(start, end, viewer, hub_color())
    for feed in feeds:
        out.extend(feed_occurrences(feed, start, end))
    out.sort(key=lambda o: (o["start"][:10], not o["all_day"], o["start"], o["title"]))
    return out
