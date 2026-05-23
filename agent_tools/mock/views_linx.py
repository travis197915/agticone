"""LINX BH claim search mock views."""
from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ._loader import load


@csrf_exempt
@require_http_methods(["POST"])
def token(request):
    return JsonResponse({
        "access_token": "mock-linx-token",
        "token_type": "Bearer",
        "expires_in": 7200,
    })


@csrf_exempt
@require_http_methods(["POST"])
def claim_search(request):
    payload = load("linx_search.json")
    return JsonResponse(payload)
