"""DOC360 mock views: OAuth token + document content read."""
from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ._loader import load


@csrf_exempt
@require_http_methods(["POST"])
def token(request):
    return JsonResponse({
        "access_token": "mock-doc360-token",
        "token_type": "Bearer",
        "expires_in": 3600,
    })


@csrf_exempt
@require_http_methods(["POST"])
def read_document_content(request):
    payload = load("doc360_content.json")
    return JsonResponse(payload)
