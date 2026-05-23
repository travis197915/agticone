"""
URL routes for the mock upstream server.

Mounted at ``/api/mocks/`` from :mod:`sop_backend.urls`. Each set of routes
corresponds to one upstream the LangChain tools call.
"""
from __future__ import annotations

from django.urls import path

from . import (
    views_cbd,
    views_cms,
    views_diagnosis,
    views_doc360,
    views_facets,
    views_fep,
    views_linx,
    views_npi,
)


app_name = "agent_tools_mocks"


urlpatterns = [
    # ── DOC360 ───────────────────────────────────────────────────────────────
    path("doc360/security/tokens",
         views_doc360.token, name="doc360-token"),
    path("doc360/api/ecs/doc360-getcontent/v1/document-contents/read",
         views_doc360.read_document_content, name="doc360-read"),

    # ── Facets ───────────────────────────────────────────────────────────────
    path("facets/security/tokens",
         views_facets.token, name="facets-token"),
    path("facets/Claims/<str:claim_number>/Inquiry/Summary",
         views_facets.summary, name="facets-summary"),
    path("facets/Claims/<str:claim_number>/Inquiry/COB",
         views_facets.cob, name="facets-cob"),
    path("facets/Claims/<str:claim_number>/Inquiry/Lines/<int:seq>/Details",
         views_facets.line_details, name="facets-line-details"),
    path("facets/Members/Coverage/MemberKey/<str:member_key>/Eligibility",
         views_facets.member_eligibility, name="facets-eligibility"),
    path("facets/data/procedure/execute",
         views_facets.procedure_execute, name="facets-procedure"),
    path("facets/Search/Claims/Inquiry",
         views_facets.search_claims, name="facets-search"),

    # ── Facet Extension Portal ───────────────────────────────────────────────
    path("fep/getCompleteList/<str:provider_id>",
         views_fep.provider_complete_list, name="fep-provider"),
    path("fep/getPrgm/<str:program_detailed_id>",
         views_fep.programme, name="fep-programme"),
    path("fep/checkModel/<str:prpr_id>",
         views_fep.group_model, name="fep-group-model"),

    # ── CBD ──────────────────────────────────────────────────────────────────
    path("cbd/oauth/token", views_cbd.token, name="cbd-token"),
    path("cbd/coverage", views_cbd.coverage, name="cbd-coverage"),
    path("cbd/customer-info", views_cbd.customer_info, name="cbd-customers"),

    # ── Diagnosis ────────────────────────────────────────────────────────────
    path("diagnosis", views_diagnosis.diagnosis, name="diagnosis"),

    # ── LINX ─────────────────────────────────────────────────────────────────
    path("linx/oauth/token", views_linx.token, name="linx-token"),
    path("linx/claim-search", views_linx.claim_search, name="linx-search"),

    # ── CMS Opt-Out ──────────────────────────────────────────────────────────
    path("cms/<str:dataset_id>/data", views_cms.opt_out_data, name="cms-optout"),

    # ── NPI ──────────────────────────────────────────────────────────────────
    path("npi/api/", views_npi.lookup, name="npi-lookup"),
]
