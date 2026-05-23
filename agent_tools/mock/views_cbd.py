"""CBD coverage mock views."""
from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ._loader import load


@csrf_exempt
@require_http_methods(["POST"])
def token(request):
    return JsonResponse({
        "access_token": "mock-cbd-token",
        "token_type": "Bearer",
        "expires_in": 3600,
    })


@csrf_exempt
@require_http_methods(["POST"])
def coverage(request):
    payload = load("cbd_coverage.json")
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["POST", "GET"])
def customer_info(request):
    return JsonResponse({
        "products": ["Medicare", "Medicaid", "Commercial"],
        "groups": ["Standard Medicare", "Standard Commercial", "RI Medicaid"],
        "plans":  ["Standard Medicare", "Standard Commercial", "RI Medicaid"],
    })
