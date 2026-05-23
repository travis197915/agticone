"""Diagnosis coverage mock view."""
from __future__ import annotations

import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ._loader import load


@csrf_exempt
@require_http_methods(["POST"])
def diagnosis(request):
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        body = {}
    filters = (body.get("filters") or [{}])[0]
    raw = ((filters.get("value") or [{}])[0]).get("filterValue", "")
    needle = (raw or "").strip().upper()

    rows = load("diagnosis_rows.json")["rows"]
    matches = [r for r in rows if r["code"].upper() == needle]
    return JsonResponse({"rows": matches, "total": len(matches)})
