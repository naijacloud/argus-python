"""URLconf for the Django tests (only imported when Django is installed)."""

from django.http import HttpResponse, JsonResponse
from django.urls import path


def order(request, pk):  # type: ignore[no-untyped-def]
    return JsonResponse({"id": pk})


def boom(request):  # type: ignore[no-untyped-def]
    raise RuntimeError("django view failed")


async def async_order(request, pk):  # type: ignore[no-untyped-def]
    return HttpResponse(str(pk))


async def async_boom(request):  # type: ignore[no-untyped-def]
    raise RuntimeError("async django view failed")


urlpatterns = [
    path("orders/<int:pk>/", order),
    path("boom/", boom),
    path("async/orders/<int:pk>/", async_order),
    path("async/boom/", async_boom),
]
