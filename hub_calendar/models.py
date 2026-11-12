from django.db import models

PALETTE = (
    "#b0304a", "#e8913a", "#e6c34a", "#3fbf7f",
    "#4f8ff0", "#8c6ff0", "#e07bd0", "#8a94a6",
)

REPEATS = (
    ("", "Never"),
    ("daily", "Every day"),
    ("weekdays", "Every weekday (Mon–Fri)"),
    ("weekly", "Every week"),
    ("biweekly", "Every 2 weeks"),
    ("monthly", "Every month"),
    ("yearly", "Every year"),
)


class Person(models.Model):
    """Someone events can be tagged with, e.g. a family member."""

    name = models.CharField(max_length=60)
    color = models.CharField(max_length=7, default=PALETTE[4])
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("order", "name")

    def __str__(self):
        return self.name

    @property
    def initials(self):
        parts = self.name.split()
        return "".join(p[0] for p in parts[:2]).upper() or "?"


class Feed(models.Model):
    """An iCal subscription (school calendar, Google, iCloud ...).

    The last good copy is kept in ``ics`` so a failed refresh never empties
    the calendar.
    """

    name = models.CharField(max_length=80)
    url = models.URLField(max_length=1000)
    color = models.CharField(max_length=7, default=PALETTE[0])
    enabled = models.BooleanField(default=True)
    people = models.ManyToManyField(Person, blank=True, related_name="feeds")
    order = models.PositiveIntegerField(default=0)
    ics = models.TextField(blank=True, default="")
    fetched_at = models.DateTimeField(null=True, blank=True)
    last_attempt = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        ordering = ("order", "id")

    def __str__(self):
        return self.name


class Event(models.Model):
    """An event added on the hub, possibly repeating.

    ``exdates`` lists the local start times (``YYYY-MM-DDTHH:MM:SS``) of
    occurrences that were deleted or moved out of the series. Events added by
    guests carry their stay's ``stay_key`` and are deleted at ``expires_at``.
    """

    title = models.CharField(max_length=200)
    start = models.DateTimeField()
    end = models.DateTimeField()
    all_day = models.BooleanField(default=False)
    location = models.CharField(max_length=200, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    repeat = models.CharField(max_length=10, blank=True, default="", choices=REPEATS)
    until = models.DateField(null=True, blank=True)
    exdates = models.JSONField(default=list, blank=True)
    people = models.ManyToManyField(Person, blank=True, related_name="events")
    stay_key = models.CharField(max_length=32, blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("start",)

    def __str__(self):
        return self.title
