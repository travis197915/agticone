"""Facet Extension Portal mock views."""
from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from ._loader import load


@require_http_methods(["GET"])
def provider_complete_list(request, provider_id: str):
    payload = load("fep_provider.json")
    payload["providerId"] = provider_id
    return JsonResponse(payload)


@require_http_methods(["GET"])
def programme(request, program_detailed_id: str):
    payload = load("fep_programme.json")
    payload["programDetailedId"] = program_detailed_id
    return JsonResponse(payload)


@require_http_methods(["GET"])
def group_model(request, prpr_id: str):
    return JsonResponse({
        "prpr_id": prpr_id,
        "group_model": "1A",
        "groupModel": "1A",
    })
