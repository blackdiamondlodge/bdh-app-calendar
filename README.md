# bdh-app-calendar

A family calendar app for [Black Diamond Hub](https://github.com/blackdiamondlodge/BlackDiamondHub).
It merges any number of iCal subscriptions (school calendars, Google, iCloud …)
with events added right on the hub.

## Features

- **Month, week and agenda views.** The week view covers the full 24 hours;
  it scrolls vertically with bouncing arrows instead of scrollbars.
- **Calendar feeds.** Subscribe to iCal/webcal links. Feeds refresh in the
  background (every 15 minutes by default) and the last good copy is kept if
  a refresh fails. Feed events are read-only.
- **Events added on the hub,** including repeating events (daily, weekdays,
  weekly, every 2 weeks, monthly, yearly) with "this event / this and
  following / all events" editing.
- **People.** Tag events and feeds with people and filter the calendar by
  person or by calendar. Colours are per feed.
- **Guests.** During a stay, guests can add events, but can only change or
  delete events they added. Their events are removed after checkout.
- **Homepage tile** showing today's count and the next few events.

## Settings (Settings → Calendar)

| Setting | |
|---|---|
| Calendar feeds | Add, colour, tag, pause, refresh or remove iCal subscriptions |
| People | The people events can be tagged with |
| Default view | Month, week or agenda |
| Week starts on | Sunday or Monday |
| Refresh feeds every | Minutes between feed refreshes (minimum 5) |
| Colour for hub events | Colour of events added on the hub |

Every hub has its own feeds, so a guest hub can stay free of personal calendars.

### Finding feed links

- **Google Calendar:** Settings → *your calendar* → Integrate calendar →
  **Secret address in iCal format**.
- **iCloud (iPhone):** Calendar app → ⓘ next to the calendar → **Public
  Calendar** → Share Link. `webcal://` links are accepted.
- **Mercer Island School District (Finalsite):**
  `https://www.mercerislandschools.org/fs/calendar-manager/events.ics?calendar_ids[]=<ID>`
  where the ID is 32 (MIHS), 14 (IMS), 30 (district), 15 (MIHS athletics),
  26 (Lakeridge), 25 (Island Park), 27 (Northwood), 28 (West Mercer).

## Development

Tests run inside a hub checkout:

```
pip install -e /path/to/bdh-app-calendar
python manage.py test hub_calendar.tests --settings=BlackDiamondHub.settings_test
```
