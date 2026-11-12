from django.urls import path

from . import views

urlpatterns = [
    path("", views.calendar_page, name="hub_calendar"),
    path("api/events", views.api_events, name="hub_calendar_api_events"),
    path("api/events/<int:pk>", views.api_event, name="hub_calendar_api_event"),
    path("api/events/<int:pk>/delete", views.api_event_delete, name="hub_calendar_api_event_delete"),
    path("settings/feeds/", views.feeds_page, name="hub_calendar_feeds"),
    path("settings/people/", views.people_page, name="hub_calendar_people"),
]
