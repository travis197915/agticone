"""NPI registry mock views."""
from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from ._loader import load


@require_http_methods(["GET"])
def lookup(request):
    payload = load("npi_provider.json")
    npi = request.GET.get("number")
    if npi:
        for entry in payload["results"]:
            entry["number"] = npi
    return JsonResponse(payload)
