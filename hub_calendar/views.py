import json
from datetime import date, datetime, time, timedelta

from bdh_sdk.auth import is_admin
from bdh_sdk.settings import app_setting
from django.db import transaction
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import cal
from .models import PALETTE, REPEATS, Event, Feed, Person

MAX_RANGE_DAYS = 120
SCOPES = ("one", "following", "all")


# ---- page ------------------------------------------------------------------

def calendar_page(request):
    viewer = cal.Viewer(request)
    config = {
        "api": reverse("hub_calendar_api_events"),
        "view": app_setting(cal.APP, "default_view", "month"),
        "weekStart": 1 if app_setting(cal.APP, "week_start", "sun") == "mon" else 0,
        "guest": viewer.guest,
        "admin": viewer.admin,
        "today": cal.now_local().date().isoformat(),
        "repeats": [list(r) for r in REPEATS],
        "settings": reverse("hub_settings_app", args=[cal.APP]) if viewer.admin else "",
        "stay": None,
    }
    stay_end_label = ""
    stay = viewer.stay
    if stay:
        arrival, checkout = stay.get("arrival"), stay.get("checkout")
        first = cal.to_local(arrival).date() if arrival else cal.now_local().date()
        if checkout:
            last = cal.to_local(checkout).date()
            config["stay"] = [first.isoformat(), last.isoformat()]
            stay_end_label = f"{last:%a} {last:%b} {last.day}"
    return render(request, "hub_calendar/calendar.html", {
        "config": config,
        "guest_name": stay.get("guest", "") if stay else "",
        "stay_end_label": stay_end_label,
    })


# ---- API ---------------------------------------------------------------------

def _bad(msg, status=400):
    return JsonResponse({"error": msg}, status=status)


def _date(value):
    return date.fromisoformat(str(value)[:10])


def api_events(request):
    if request.method == "POST":
        return _create(request)
    try:
        start = _date(request.GET["start"])
        end = _date(request.GET["end"])
    except (KeyError, ValueError):
        return _bad("start and end dates are required")
    if end <= start or (end - start).days > MAX_RANGE_DAYS:
        return _bad("bad date range")
    cal.purge_expired()
    cal.kick_refresh()
    viewer = cal.Viewer(request)
    feeds = list(Feed.objects.filter(enabled=True).prefetch_related("people"))
    events = cal.occurrences(datetime.combine(start, time()), datetime.combine(end, time()),
                             viewer, feeds)
    now = cal.now_local()
    return JsonResponse({
        "events": events,
        "feeds": [{"id": f"feed:{f.id}", "name": f.name, "color": f.color,
                   "error": bool(f.last_error)} for f in feeds],
        "hub": {"id": "hub", "name": "Added on the hub", "color": cal.hub_color()},
        "people": [{"id": p.id, "name": p.name, "initials": p.initials, "color": p.color}
                   for p in Person.objects.all()],
        "me": {"guest": viewer.guest, "admin": viewer.admin},
        "today": now.date().isoformat(),
        "now": cal.iso(now),
    })


def _parse(data):
    """Validate an event payload -> dict of model fields (local naive times)."""
    title = str(data.get("title", "")).strip()[:200]
    if not title:
        raise ValueError("Give the event a title.")
    all_day = bool(data.get("all_day"))
    try:
        if all_day:
            s = datetime.combine(_date(data["start"]), time())
            last = _date(data.get("end") or data["start"])
            e = datetime.combine(last, time()) + timedelta(days=1)
        else:
            s = datetime.fromisoformat(str(data["start"]))
            e = datetime.fromisoformat(str(data.get("end") or data["start"]))
    except (KeyError, ValueError):
        raise ValueError("That date or time doesn't look right.")
    s, e = s.replace(tzinfo=None, second=0, microsecond=0), e.replace(tzinfo=None, second=0, microsecond=0)
    if e < s:
        raise ValueError("The event ends before it starts.")
    repeat = str(data.get("repeat") or "")
    if repeat not in dict(REPEATS):
        raise ValueError("Unknown repeat option.")
    until = None
    if repeat and data.get("until"):
        try:
            until = _date(data["until"])
        except ValueError:
            raise ValueError("The repeat end date doesn't look right.")
        if until < s.date():
            raise ValueError("The repeat ends before the event starts.")
    people = [int(p) for p in data.get("people") or [] if str(p).isdigit()]
    return {
        "title": title, "start": s, "end": e, "all_day": all_day,
        "location": str(data.get("location", "")).strip()[:200],
        "notes": str(data.get("notes", "")).strip()[:4000],
        "repeat": repeat, "until": until,
        "people": list(Person.objects.filter(id__in=people)),
    }


def _payload(request):
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        raise ValueError("Bad request.")
    if not isinstance(data, dict):
        raise ValueError("Bad request.")
    return data


def _save(event, fields):
    people = fields.pop("people")
    event.title = fields["title"]
    event.start = cal.from_local(fields["start"])
    event.end = cal.from_local(fields["end"])
    event.all_day = fields["all_day"]
    event.location = fields["location"]
    event.notes = fields["notes"]
    event.repeat = fields["repeat"]
    event.until = fields["until"] if fields["repeat"] else None
    if not event.repeat:
        event.exdates = []
    event.save()
    event.people.set(people)
    return event


def _create(request):
    viewer = cal.Viewer(request)
    try:
        fields = _parse(_payload(request))
    except ValueError as exc:
        return _bad(str(exc))
    with transaction.atomic():
        event = _save(Event(**viewer.new_event_fields()), fields)
    return JsonResponse({"ok": True, "id": event.id})


def _editable(request, pk):
    viewer = cal.Viewer(request)
    event = get_object_or_404(Event, pk=pk)
    if not viewer.can_edit(event):
        return None, viewer
    return event, viewer


def _occ(data, event):
    """The occurrence being changed (local naive datetime)."""
    try:
        return datetime.fromisoformat(str(data.get("occ"))).replace(tzinfo=None)
    except (TypeError, ValueError):
        return cal.to_local(event.start)


def _clone(series, **extra):
    """A new event owned by the same person/stay as ``series``."""
    return Event(stay_key=series.stay_key, expires_at=series.expires_at, **extra)


def _end_series_before(series, occ):
    """Stop ``series`` before the occurrence at ``occ``."""
    series.until = occ.date() - timedelta(days=1)
    series.exdates = [x for x in series.exdates if x < cal.iso(occ)]
    series.save(update_fields=["until", "exdates"])


@require_POST
def api_event(request, pk):
    event, viewer = _editable(request, pk)
    if event is None:
        return HttpResponseForbidden("You can only change events you added.")
    try:
        data = _payload(request)
        fields = _parse(data)
    except ValueError as exc:
        return _bad(str(exc))
    scope = data.get("scope") if data.get("scope") in SCOPES else "all"
    series_start = cal.to_local(event.start)
    occ = _occ(data, event)
    with transaction.atomic():
        if not event.repeat or scope == "all" or (scope == "following" and occ <= series_start):
            if event.repeat and fields["repeat"] and scope == "all":
                # Moving one occurrence's time moves the whole series by the same amount.
                delta = fields["start"] - occ
                dur = fields["end"] - fields["start"]
                fields["start"] = series_start + delta
                fields["end"] = fields["start"] + dur
                event.exdates = [cal.iso(datetime.fromisoformat(x) + delta) for x in event.exdates]
            _save(event, fields)
            return JsonResponse({"ok": True, "id": event.id})
        if scope == "one":
            event.exdates = sorted(set(event.exdates) | {cal.iso(occ)})
            event.save(update_fields=["exdates"])
            fields["repeat"], fields["until"] = "", None
            new = _save(_clone(event), fields)
            return JsonResponse({"ok": True, "id": new.id})
        # "following": end the old series and start a new one from here.
        delta = fields["start"] - occ
        carried = [cal.iso(datetime.fromisoformat(x) + delta) for x in event.exdates if x > cal.iso(occ)]
        _end_series_before(event, occ)
        new = _clone(event, exdates=carried)
        _save(new, fields)
        return JsonResponse({"ok": True, "id": new.id})


@require_POST
def api_event_delete(request, pk):
    event, viewer = _editable(request, pk)
    if event is None:
        return HttpResponseForbidden("You can only delete events you added.")
    try:
        data = _payload(request)
    except ValueError as exc:
        return _bad(str(exc))
    scope = data.get("scope") if data.get("scope") in SCOPES else "all"
    occ = _occ(data, event)
    with transaction.atomic():
        if not event.repeat or scope == "all" or (scope == "following" and occ <= cal.to_local(event.start)):
            event.delete()
        elif scope == "one":
            event.exdates = sorted(set(event.exdates) | {cal.iso(occ)})
            event.save(update_fields=["exdates"])
        else:
            _end_series_before(event, occ)
    return JsonResponse({"ok": True})


# ---- settings pages ----------------------------------------------------------

def _admin_only(view):
    def wrapped(request, *args, **kwargs):
        if not is_admin(request):
            raise Http404()
        return view(request, *args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def _color(value, fallback):
    value = str(value or "").strip()
    return value if len(value) == 7 and value.startswith("#") else fallback


def _feed_from_post(feed, post):
    name = post.get("name", "").strip()[:80]
    url = cal.normalize_url(post.get("url", ""))
    if not name or not url.lower().startswith(("http://", "https://")):
        raise ValueError("A feed needs a name and an http(s) or webcal link.")
    url_changed = url != feed.url
    feed.name, feed.url = name, url[:1000]
    feed.color = _color(post.get("color"), feed.color)
    feed.enabled = post.get("enabled") == "on"
    if url_changed:
        feed.ics, feed.fetched_at, feed.last_attempt, feed.last_error = "", None, None, ""
    feed.save()
    feed.people.set(Person.objects.filter(id__in=post.getlist("people")))
    return url_changed


def _ago(dt):
    if not dt:
        return "never"
    mins = int((timezone.now() - dt).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 60 * 48:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} days ago"


@_admin_only
def feeds_page(request):
    note = None
    if request.method == "POST":
        action = request.POST.get("action")
        feed = None
        if request.POST.get("id"):
            feed = get_object_or_404(Feed, pk=request.POST["id"])
        try:
            if action == "add":
                feed = Feed(order=Feed.objects.count(), enabled=True,
                            color=PALETTE[Feed.objects.count() % len(PALETTE)])
                data = request.POST.copy()
                data["enabled"] = "on"
                _feed_from_post(feed, data)
                ok = cal.refresh_feed(feed)
                note = ("ok", f"Added {feed.name}.") if ok else (
                    "bad", f"Added {feed.name}, but it couldn't be loaded: {feed.last_error}")
            elif action == "save" and feed:
                if _feed_from_post(feed, request.POST) and feed.enabled:
                    cal.refresh_feed(feed)
                note = ("ok", f"Saved {feed.name}.")
            elif action == "refresh" and feed:
                ok = cal.refresh_feed(feed)
                note = ("ok", f"Refreshed {feed.name}.") if ok else (
                    "bad", f"{feed.name} couldn't be loaded: {feed.last_error}")
            elif action == "delete" and feed:
                feed.delete()
                note = ("ok", f"Removed {feed.name}.")
        except ValueError as exc:
            note = ("bad", str(exc))
        if note and note[0] == "ok":
            request.session["hub_calendar_note"] = note
            return redirect("hub_calendar_feeds")
    note = note or request.session.pop("hub_calendar_note", None)
    feeds = list(Feed.objects.prefetch_related("people"))
    for f in feeds:
        f.updated = _ago(f.fetched_at)
        f.tagged = {p.id for p in f.people.all()}
    return render(request, "hub_calendar/settings_feeds.html", {
        "active": cal.APP, "feeds": feeds, "people": Person.objects.all(),
        "palette": PALETTE, "note": note, "next_color": PALETTE[len(feeds) % len(PALETTE)],
    })


@_admin_only
def people_page(request):
    note = None
    if request.method == "POST":
        action = request.POST.get("action")
        name = request.POST.get("name", "").strip()[:60]
        person = get_object_or_404(Person, pk=request.POST["id"]) if request.POST.get("id") else None
        if action == "add":
            if name:
                n = Person.objects.count()
                Person.objects.create(name=name, order=n,
                                      color=_color(request.POST.get("color"), PALETTE[(n + 4) % len(PALETTE)]))
                note = ("ok", f"Added {name}.")
            else:
                note = ("bad", "Enter a name.")
        elif action == "save" and person:
            if name:
                person.name = name
                person.color = _color(request.POST.get("color"), person.color)
                person.save()
                note = ("ok", f"Saved {name}.")
            else:
                note = ("bad", "Enter a name.")
        elif action == "delete" and person:
            person.delete()
            note = ("ok", f"Removed {person.name}.")
        if note and note[0] == "ok":
            request.session["hub_calendar_note"] = note
            return redirect("hub_calendar_people")
    note = note or request.session.pop("hub_calendar_note", None)
    return render(request, "hub_calendar/settings_people.html", {
        "active": cal.APP, "people": Person.objects.all(), "palette": PALETTE, "note": note,
        "next_color": PALETTE[(Person.objects.count() + 4) % len(PALETTE)],
    })
