"""Homepage tile: what's on today and next."""
import logging
from datetime import datetime, time, timedelta

from bdh_sdk import Status, StatusRow
from bdh_sdk.plugins import INFO, ON

from . import cal

log = logging.getLogger(__name__)

ROWS = 4
LOOKAHEAD_DAYS = 7


class _Reader:
    """Status has no request; nothing on the tile is editable."""

    guest = False

    def can_edit(self, event):
        return False


def _when(o, today):
    s = datetime.fromisoformat(o["start"])
    day = s.date()
    if day == today:
        label = "Today"
    elif day == today + timedelta(days=1):
        label = "Tomorrow"
    else:
        label = f"{s:%a} {s:%b} {day.day}"
    if o["all_day"]:
        return f"{label} · all day"
    hour = s.strftime("%I").lstrip("0")
    minute = s.strftime(":%M") if s.minute else ""
    return f"{label} · {hour}{minute} {s:%p}"


def status(mode=None):
    try:
        cal.purge_expired()
        cal.kick_refresh()
        now = cal.now_local()
        today = now.date()
        start = datetime.combine(today, time())
        events = cal.occurrences(start, start + timedelta(days=LOOKAHEAD_DAYS), _Reader())
    except Exception:
        log.exception("calendar status failed")
        return None
    todays = [o for o in events if o["start"][:10] <= today.isoformat() < o["end"][:10]
              or o["start"][:10] == today.isoformat()]
    upcoming = [o for o in events
                if (o["all_day"] and o["end"][:10] > today.isoformat())
                or (not o["all_day"] and datetime.fromisoformat(o["end"]) > now)]
    rows = tuple(
        StatusRow(id=o["id"], title=o["title"], detail=_when(o, today),
                  dimmed=o["start"][:10] != today.isoformat())
        for o in upcoming[:ROWS]
    )
    n = len(todays)
    text = f"{n} today" if n else ("Nothing today" if rows else "Nothing this week")
    return Status(text=text, level=ON if n else INFO, rows=rows, refresh=300)
