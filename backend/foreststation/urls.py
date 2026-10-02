"""Root URL configuration: REST API + health check."""
from django.http import JsonResponse
from django.urls import include, path


def health(_request):
    return JsonResponse({"status": "ok", "service": "forest-station"})


urlpatterns = [
    path("api/health/", health),
    path("api/", include("inventory.urls")),
]
