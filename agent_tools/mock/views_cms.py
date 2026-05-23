"""CMS Provider Opt-Out Affidavits mock views."""
from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from ._loader import load


@require_http_methods(["GET"])
def opt_out_data(request, dataset_id: str):
    """Mirror the CMS opt-out filter surface the upstream tool uses.

    Supported query params (any combination):

    * ``filter[NPI]=<npi>``
    * ``filter[filter-0][condition][path]=Last Name`` with
      ``filter[filter-0][condition][value]=<last>`` — last-name match
      (case-insensitive). Index 1 / 2 mirror first name + state code.
    """
    records = load("optout_records.json")["records"]
    npi = request.GET.get("filter[NPI]")
    if npi:
        records = [r for r in records if str(r.get("NPI")) == str(npi)]

    # Generic filter[filter-N] pattern — translate into in-memory matches.
    # The upstream API encodes (path, value) pairs by index; we honor
    # whichever indices are present.
    fields_by_path = {
        "Last Name":   "Last Name",
        "First Name":  "First Name",
        "State Code":  "State Code",
        "Specialty":   "Specialty",
    }
    for idx in range(8):
        path  = request.GET.get(f"filter[filter-{idx}][condition][path]")
        value = request.GET.get(f"filter[filter-{idx}][condition][value]")
        if not path or value is None:
            continue
        field = fields_by_path.get(path)
        if not field:
            continue
        v = str(value).strip().upper()
        records = [
            r for r in records
            if str(r.get(field, "")).strip().upper() == v
        ]

    return JsonResponse({"records": records, "total": len(records)})
