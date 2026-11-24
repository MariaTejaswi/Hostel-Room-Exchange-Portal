from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import RedirectView

urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "favicon.ico",
        RedirectView.as_view(url=settings.STATIC_URL + "images/logo.svg", permanent=True),
        name="favicon",
    ),
    path("", RedirectView.as_view(pattern_name="accounts:login", permanent=False), name="home"),
    path("", include("apps.accounts.urls", namespace="accounts")),
    path("", include("apps.hostels.urls", namespace="hostels")),
    path("", include("apps.exchange.urls", namespace="exchange")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
