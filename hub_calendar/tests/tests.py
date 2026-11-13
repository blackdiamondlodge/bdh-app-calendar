import json
from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hub_calendar import cal, homepage
from hub_calendar.models import Event, Feed, Person

TZ = {"timezone": "America/New_York"}

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
X-WR-TIMEZONE:America/New_York
BEGIN:VEVENT
UID:practice@test
DTSTART:20250901T220000Z
DTEND:20250901T233000Z
RRULE:FREQ=WEEKLY;COUNT=10
EXDATE:20250908T220000Z
SUMMARY:Practice
LOCATION:Field 2
END:VEVENT
BEGIN:VEVENT
UID:noschool@test
DTSTART;VALUE=DATE:20250903
DTEND;VALUE=DATE:20250905
SUMMARY:No school
END:VEVENT
END:VCALENDAR
"""


def L(s):
    return datetime.fromisoformat(s)


class _Viewer:
    guest = False

    def can_edit(self, event):
        return True


@override_settings(HUB_LOCATION=TZ)
class FeedTests(TestCase):
    def setUp(self):
        cal._parsed.clear()
        self.feed = Feed.objects.create(name="MIHS", url="https://x/ics", color="#4f8ff0", ics=ICS,
                                        fetched_at=timezone.now())

    def test_recurring_feed_events_in_local_time_with_exdate(self):
        evs = cal.feed_occurrences(self.feed, L("2025-09-01T00:00"), L("2025-09-29T00:00"))
        practice = [e["start"] for e in evs if e["title"] == "Practice"]
        # X-WR-TIMEZONE: 22:00 UTC is 6 pm in New York; Sep 8 was cancelled.
        self.assertEqual(practice, ["2025-09-01T18:00:00", "2025-09-15T18:00:00", "2025-09-22T18:00:00"])
        e = next(e for e in evs if e["title"] == "Practice")
        self.assertEqual((e["end"], e["location"], e["editable"], e["source"]),
                         ("2025-09-01T19:30:00", "Field 2", False, f"feed:{self.feed.id}"))

    def test_all_day_feed_event(self):
        evs = cal.feed_occurrences(self.feed, L("2025-09-04T00:00"), L("2025-09-05T00:00"))
        self.assertEqual([(e["title"], e["all_day"], e["start"], e["end"]) for e in evs],
                         [("No school", True, "2025-09-03T00:00:00", "2025-09-05T00:00:00")])

    def test_webcal_links_become_https(self):
        self.assertEqual(cal.normalize_url(" webcal://p01.icloud.com/x "), "https://p01.icloud.com/x")

    def test_failed_refresh_keeps_last_good_copy(self):
        with patch.object(cal, "fetch_ics", side_effect=OSError("timed out")):
            self.assertFalse(cal.refresh_feed(self.feed))
        self.feed.refresh_from_db()
        self.assertEqual((self.feed.ics, self.feed.last_error), (ICS, "timed out"))
        with patch.object(cal, "fetch_ics", return_value=ICS):
            self.assertTrue(cal.refresh_feed(self.feed))
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.last_error, "")

    def test_disabled_feeds_are_not_shown(self):
        self.feed.enabled = False
        self.feed.save()
        self.assertEqual(cal.occurrences(L("2025-09-01T00:00"), L("2025-09-29T00:00"), _Viewer()), [])


@override_settings(HUB_LOCATION=TZ)
class HubRecurrenceTests(TestCase):
    def ev(self, start, end, **kw):
        return Event.objects.create(title=kw.pop("title", "Piano"), start=cal.from_local(L(start)),
                                    end=cal.from_local(L(end)), **kw)

    def starts(self, a, b):
        return [o["start"] for o in cal.occurrences(L(a), L(b), _Viewer())]

    def test_weekly_keeps_wall_clock_time_across_dst(self):
        self.ev("2025-10-27T19:00", "2025-10-27T20:00", repeat="weekly", until=date(2025, 11, 10),
                exdates=["2025-11-03T19:00:00"])
        self.assertEqual(self.starts("2025-10-20T00:00", "2025-12-01T00:00"),
                         ["2025-10-27T19:00:00", "2025-11-10T19:00:00"])

    def test_all_day_event_on_dst_change_stays_one_day(self):
        self.ev("2025-11-02T00:00", "2025-11-03T00:00", all_day=True, title="Ski")
        [o] = cal.occurrences(L("2025-11-01T00:00"), L("2025-11-05T00:00"), _Viewer())
        self.assertEqual((o["start"], o["end"]), ("2025-11-02T00:00:00", "2025-11-03T00:00:00"))

    def test_weekdays_and_overlap_from_before_range(self):
        self.ev("2025-09-05T08:00", "2025-09-05T09:00", repeat="weekdays")  # a Friday
        self.ev("2025-09-07T23:00", "2025-09-08T01:00", title="Late")
        self.assertEqual(self.starts("2025-09-08T00:00", "2025-09-10T00:00"),
                         ["2025-09-07T23:00:00", "2025-09-08T08:00:00", "2025-09-09T08:00:00"])


@override_settings(HUB_LOCATION=TZ)
class ApiTests(TestCase):
    def setUp(self):
        p = patch.object(cal, "kick_refresh", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        self.url = reverse("hub_calendar_api_events")
        self.admin = get_user_model().objects.create_user("owner", password="x", is_staff=True)

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def get_events(self, start="2025-09-01", end="2025-10-01"):
        r = self.client.get(self.url, {"start": start, "end": end})
        self.assertEqual(r.status_code, 200)
        return r.json()["events"]

    def weekly(self, **kw):
        body = {"title": "Piano", "start": "2025-09-01T16:00", "end": "2025-09-01T17:00", "repeat": "weekly"}
        body.update(kw)
        r = self.post(self.url, body)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["id"]

    def test_create_all_day_takes_inclusive_end(self):
        r = self.post(self.url, {"title": "Trip", "all_day": True, "start": "2025-09-05", "end": "2025-09-07"})
        self.assertEqual(r.status_code, 200)
        [e] = self.get_events()
        self.assertEqual((e["start"], e["end"], e["all_day"]), ("2025-09-05T00:00:00", "2025-09-08T00:00:00", True))

    def test_validation(self):
        self.assertEqual(self.post(self.url, {"title": " ", "start": "2025-09-01T10:00"}).status_code, 400)
        r = self.post(self.url, {"title": "x", "start": "2025-09-01T10:00", "end": "2025-09-01T09:00"})
        self.assertEqual(r.json()["error"], "The event ends before it starts.")
        self.assertEqual(self.client.get(self.url, {"start": "2025-01-01", "end": "2026-01-01"}).status_code, 400)

    def test_edit_this_event_only(self):
        pk = self.weekly()
        r = self.post(reverse("hub_calendar_api_event", args=[pk]),
                      {"title": "Piano recital", "start": "2025-09-08T18:00", "end": "2025-09-08T19:00",
                       "repeat": "weekly", "scope": "one", "occ": "2025-09-08T16:00:00"})
        self.assertEqual(r.status_code, 200)
        evs = [(e["start"], e["title"]) for e in self.get_events("2025-09-01", "2025-09-16")]
        self.assertEqual(evs, [("2025-09-01T16:00:00", "Piano"), ("2025-09-08T18:00:00", "Piano recital"),
                               ("2025-09-15T16:00:00", "Piano")])

    def test_edit_this_and_following(self):
        pk = self.weekly()
        self.post(reverse("hub_calendar_api_event", args=[pk]),
                  {"title": "Piano", "start": "2025-09-15T17:00", "end": "2025-09-15T18:00",
                   "repeat": "weekly", "scope": "following", "occ": "2025-09-15T16:00:00"})
        self.assertEqual([e["start"] for e in self.get_events("2025-09-01", "2025-09-23")],
                         ["2025-09-01T16:00:00", "2025-09-08T16:00:00", "2025-09-15T17:00:00", "2025-09-22T17:00:00"])
        self.assertEqual(Event.objects.get(pk=pk).until, date(2025, 9, 14))

    def test_edit_all_from_a_later_occurrence_moves_the_series(self):
        pk = self.weekly()
        self.post(reverse("hub_calendar_api_event", args=[pk]),
                  {"title": "Piano", "start": "2025-09-09T16:30", "end": "2025-09-09T17:30",
                   "repeat": "weekly", "scope": "all", "occ": "2025-09-08T16:00:00"})
        self.assertEqual([e["start"] for e in self.get_events("2025-09-01", "2025-09-16")],
                         ["2025-09-02T16:30:00", "2025-09-09T16:30:00"])

    def test_delete_scopes(self):
        pk = self.weekly(until="2025-09-29")
        url = reverse("hub_calendar_api_event_delete", args=[pk])
        self.post(url, {"scope": "one", "occ": "2025-09-08T16:00:00"})
        self.post(url, {"scope": "following", "occ": "2025-09-22T16:00:00"})
        self.assertEqual([e["start"] for e in self.get_events()], ["2025-09-01T16:00:00", "2025-09-15T16:00:00"])
        self.post(url, {"scope": "all"})
        self.assertFalse(Event.objects.exists())

    def guest(self, guest="Smiths"):
        stay = {"guest": guest, "arrival": timezone.make_aware(datetime(2025, 9, 1, 16)),
                "checkout": timezone.now() + timedelta(days=2), "owner": False}
        return patch.object(cal, "_current_stay", return_value=stay)

    def test_guests_only_change_their_own_events(self):
        owner = self.weekly()
        with self.guest():
            mine = self.post(self.url, {"title": "Hot tub", "start": "2025-09-02T20:00", "end": "2025-09-02T21:00"}).json()["id"]
            ev = Event.objects.get(pk=mine)
            self.assertTrue(ev.stay_key and ev.expires_at)
            evs = {e["title"]: e["editable"] for e in self.get_events("2025-09-01", "2025-09-03")}
            self.assertEqual(evs, {"Piano": False, "Hot tub": True})
            self.assertEqual(self.post(reverse("hub_calendar_api_event_delete", args=[owner]), {}).status_code, 403)
            self.assertEqual(self.post(reverse("hub_calendar_api_event", args=[owner]),
                                       {"title": "x", "start": "2025-09-01T10:00"}).status_code, 403)
            self.assertEqual(self.post(reverse("hub_calendar_api_event_delete", args=[mine]), {}).status_code, 200)
        with self.guest("Joneses"):
            other = self.post(self.url, {"title": "Ski", "start": "2025-09-03T09:00"}).json()["id"]
        with self.guest():
            self.assertEqual(self.post(reverse("hub_calendar_api_event_delete", args=[other]), {}).status_code, 403)
        # The owner can change anything added on the hub.
        self.client.force_login(self.admin)
        self.assertEqual(self.post(reverse("hub_calendar_api_event_delete", args=[other]), {}).status_code, 200)

    def test_guest_events_are_removed_after_checkout(self):
        e = Event.objects.create(title="Old", start=timezone.now(), end=timezone.now(), stay_key="abc",
                                 expires_at=timezone.now() - timedelta(minutes=1))
        Event.objects.create(title="Keep", start=timezone.now(), end=timezone.now())
        self.get_events()
        self.assertFalse(Event.objects.filter(pk=e.pk).exists())
        self.assertTrue(Event.objects.filter(title="Keep").exists())

    def test_page_renders(self):
        r = self.client.get(reverse("hub_calendar"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'id="cal-config"')
        with self.guest():
            self.assertContains(self.client.get(reverse("hub_calendar")), "removed after checkout")


@override_settings(HUB_LOCATION=TZ)
class SettingsPageTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user("owner", password="x", is_staff=True)

    def test_admin_only(self):
        self.assertEqual(self.client.get(reverse("hub_calendar_feeds")).status_code, 404)
        self.assertEqual(self.client.get(reverse("hub_calendar_people")).status_code, 404)

    def test_add_person_and_feed(self):
        self.client.force_login(self.admin)
        r = self.client.post(reverse("hub_calendar_people"), {"action": "add", "name": "Ava Slivinski", "color": "#e07bd0"})
        self.assertRedirects(r, reverse("hub_calendar_people"))
        ava = Person.objects.get()
        self.assertEqual((ava.initials, ava.color), ("AS", "#e07bd0"))
        with patch.object(cal, "fetch_ics", return_value=ICS) as fetch:
            r = self.client.post(reverse("hub_calendar_feeds"),
                                 {"action": "add", "name": "MIHS", "url": "webcal://example.org/mihs.ics",
                                  "people": [ava.id]})
        self.assertRedirects(r, reverse("hub_calendar_feeds"), fetch_redirect_response=False)
        fetch.assert_called_once_with("https://example.org/mihs.ics")
        feed = Feed.objects.get()
        self.assertEqual((feed.ics, list(feed.people.all())), (ICS, [ava]))
        page = self.client.get(reverse("hub_calendar_feeds"))
        self.assertContains(page, "Added MIHS.")
        self.assertContains(page, "Updated just now")
        self.assertContains(page, '<details class="hst-card hc-feed" open>')
        self.assertContains(page, 'id="hc-add" hidden')
        self.assertNotContains(self.client.get(reverse("hub_calendar_feeds")), "<details class=\"hst-card hc-feed\" open>")

    def test_rejected_new_feed_keeps_the_add_form_open(self):
        self.client.force_login(self.admin)
        Feed.objects.create(name="Old", url="https://example.org/old.ics", order=0)
        r = self.client.post(reverse("hub_calendar_feeds"), {"action": "add", "name": "Bad", "url": "ftp://example.org/x"})
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'id="hc-add" hidden')
        self.assertContains(r, 'id="hc-addbtn" hidden')

    def test_feed_that_fails_to_load_is_kept_with_error(self):
        self.client.force_login(self.admin)
        with patch.object(cal, "fetch_ics", side_effect=ValueError("that link didn't return an iCal calendar")):
            r = self.client.post(reverse("hub_calendar_feeds"),
                                 {"action": "add", "name": "Bad", "url": "https://example.org/x"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "couldn&#x27;t be loaded")
        self.assertTrue(Feed.objects.filter(name="Bad").exists())


@override_settings(HUB_LOCATION=TZ)
class StatusTests(TestCase):
    def test_tile_summarises_today_and_upcoming(self):
        now = cal.now_local().replace(second=0, microsecond=0)
        later = now + timedelta(hours=1)
        if later.date() != now.date():
            later = now
        Event.objects.create(title="Dinner", start=cal.from_local(later), end=cal.from_local(later + timedelta(hours=1)))
        tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        Event.objects.create(title="Ski", start=cal.from_local(tomorrow), end=cal.from_local(tomorrow + timedelta(days=1)),
                             all_day=True)
        with patch.object(cal, "kick_refresh", return_value=False):
            s = homepage.status()
        self.assertEqual(s.text, "1 today")
        self.assertEqual([(r.title, r.dimmed) for r in s.rows], [("Dinner", False), ("Ski", True)])
        self.assertEqual(s.rows[1].detail, "Tomorrow · all day")

    def test_empty(self):
        with patch.object(cal, "kick_refresh", return_value=False):
            self.assertEqual(homepage.status().text, "Nothing this week")


class SiteTzTests(TestCase):
    def test_follows_hub_location(self):
        from bdh_sdk.location import Location
        with patch.object(cal, "hub_location", return_value=Location(timezone="America/Vancouver")):
            self.assertEqual(str(cal.site_tz()), "America/Vancouver")

    @override_settings(TIME_ZONE="America/Toronto")
    def test_falls_back_to_django_time_zone(self):
        with patch.object(cal, "hub_location", return_value=None):
            self.assertEqual(str(cal.site_tz()), "America/Toronto")

