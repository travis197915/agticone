"""Facets mock views: token + summary + COB + line details + eligibility + provider + search."""
from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ._loader import load


@csrf_exempt
@require_http_methods(["POST"])
def token(request):
    return JsonResponse({
        "access_token": "mock-facets-token",
        "token_type": "Bearer",
        "expires_in": 3000,
    })


@require_http_methods(["GET"])
def summary(request, claim_number: str):
    payload = load("facets_summary.json")
    return JsonResponse(payload)


@require_http_methods(["GET"])
def cob(request, claim_number: str):
    payload = load("facets_cob.json")
    return JsonResponse(payload)


@require_http_methods(["GET"])
def line_details(request, claim_number: str, seq: int):
    # We expose two lines (1 and 2) and 404 for seq >= 3 so the iterating
    # tool terminates cleanly.
    if int(seq) >= 3:
        return JsonResponse({"error": "not found"}, status=404)
    payload = load("facets_line.json")
    payload["CDML_SEQ_NO"] = int(seq)
    return JsonResponse(payload)


@require_http_methods(["GET"])
def member_eligibility(request, member_key: str):
    payload = load("facets_eligibility.json")
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["POST"])
def procedure_execute(request):
    payload = load("facets_provider_rows.json")
    return JsonResponse(payload)


@require_http_methods(["GET"])
def search_claims(request):
    payload = load("facets_duplicate.json")
    return JsonResponse(payload)
