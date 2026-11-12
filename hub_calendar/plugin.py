from bdh_sdk import PluginSpec, Setting, SettingsPage, Tile
from bdh_sdk.plugins import CHOICE, INT

spec = PluginSpec(
    id="hub_calendar",
    app="hub_calendar",
    urls="hub_calendar.urls",
    url_prefix="calendar/",
    status="hub_calendar.homepage.status",
    tiles=(
        Tile(
            title="Calendar",
            url_name="hub_calendar",
            icon="hub_calendar/hub/icon.svg",
            section="lodge",
            subtitle="Family calendar and events",
            order=15,
        ),
    ),
    settings=(
        Setting("default_view", "Opens in", type=CHOICE, default="month",
                choices=(("month", "Month"), ("week", "Week"), ("agenda", "Agenda"))),
        Setting("week_start", "Week starts on", type=CHOICE, default="sun",
                choices=(("sun", "Sunday"), ("mon", "Monday"))),
        Setting("refresh", "Refresh feeds every", type=INT, default=15,
                minimum=5, maximum=1440, unit="min"),
        Setting("hub_color", "Colour of hub events", default="#3fbf7f",
                help="Events added on this hub, as a hex colour like #3fbf7f."),
    ),
    settings_pages=(
        SettingsPage("Calendar feeds", "hub_calendar_feeds",
                     subtitle="iCal links (school, Google, iCloud) shown on this hub",
                     icon="fas fa-rss"),
        SettingsPage("People", "hub_calendar_people",
                     subtitle="Who events can be tagged with", icon="fas fa-user-tag"),
    ),
)