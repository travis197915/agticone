from django.urls import path
from .views import (HealthView, JobListCreateView, JobDetailView, SyncRunView,
                    JobGraphView, JobSectionsView, JobContextualizeView,
                    ViewerJobListView, ViewerJobDetailView, ViewerDocDetailView)
from .exclusion_views import (SopExclusionListCreateView,
                              SopExclusionDetailView,
                              SopExclusionToggleView,
                              SopHtmlBlocksView,
                              SopSourceHtmlView)
from .version_views import (
    RevisionCheckView,
    SopDocumentAffectedWorkflowsView,
    SopDocumentRevisionCheckView,
    SopDocumentVersionsView,
    SopVersionActivateView,
    SopVersionRejectView,
    SopVersionDiffView,
)
from .dom_tree_views import SopDomTreeView, SopDomBlockView

app_name = "sop_ingestion"


def _slashless(name: str) -> str:
    return f"{name}-no-slash"


urlpatterns = [
    # REST API
    path("",                     JobListCreateView.as_view(), name="job-list-create"),
    path("health/",              HealthView.as_view(),        name="health"),
    path("run-sync/",            SyncRunView.as_view(),       name="run-sync"),
    path("revision-check/",      RevisionCheckView.as_view(), name="revision-check"),
    path("revision-check",       RevisionCheckView.as_view(), name=_slashless("revision-check")),
    path("<uuid:job_id>/",       JobDetailView.as_view(),     name="job-detail"),
    path("<uuid:job_id>/graph/",    JobGraphView.as_view(),    name="job-graph"),
    path("<uuid:job_id>/sections/",      JobSectionsView.as_view(),      name="job-sections"),
    path("<uuid:job_id>/contextualize/", JobContextualizeView.as_view(), name="job-contextualize"),

    path("documents/<int:document_id>/versions/",
         SopDocumentVersionsView.as_view(),
         name="sop-document-versions"),
    path("documents/<int:document_id>/affected-workflows/",
         SopDocumentAffectedWorkflowsView.as_view(),
         name="sop-document-affected-workflows"),
    path("documents/<int:document_id>/affected-workflows",
         SopDocumentAffectedWorkflowsView.as_view(),
         name=_slashless("sop-document-affected-workflows")),
    path("documents/<int:document_id>/revision-check/",
         SopDocumentRevisionCheckView.as_view(),
         name="sop-document-revision-check"),
    path("documents/<int:document_id>/revision-check",
         SopDocumentRevisionCheckView.as_view(),
         name=_slashless("sop-document-revision-check")),
    path("sops/<int:sop_id>/diff/",
         SopVersionDiffView.as_view(),
         name="sop-version-diff"),
    path("sops/<int:sop_id>/diff",
         SopVersionDiffView.as_view(),
         name=_slashless("sop-version-diff")),
    path("sops/<int:sop_id>/activate/",
         SopVersionActivateView.as_view(),
         name="sop-version-activate"),
    path("sops/<int:sop_id>/activate",
         SopVersionActivateView.as_view(),
         name=_slashless("sop-version-activate")),
    path("sops/<int:sop_id>/reject/",
         SopVersionRejectView.as_view(),
         name="sop-version-reject"),
    path("sops/<int:sop_id>/reject",
         SopVersionRejectView.as_view(),
         name=_slashless("sop-version-reject")),

    # User-curated exclusions (per-SOP, no LLM)
    path("sops/<int:sop_id>/exclusions/",
         SopExclusionListCreateView.as_view(),
         name="sop-exclusions-list-create"),
    path("sops/<int:sop_id>/exclusions/toggle/",
         SopExclusionToggleView.as_view(),
         name="sop-exclusions-toggle"),
    path("sops/<int:sop_id>/exclusions/<int:exclusion_id>/",
         SopExclusionDetailView.as_view(),
         name="sop-exclusions-detail"),
    path("sops/<int:sop_id>/html-blocks/",
         SopHtmlBlocksView.as_view(),
         name="sop-html-blocks"),
    path("sops/<int:sop_id>/source-html/",
         SopSourceHtmlView.as_view(),
         name="sop-source-html"),

    # HTML DOM-mirror tree (mirrors the source HTML structure 1:1, served
    # straight from Neo4j HtmlBlock nodes with :DERIVED_FROM semantic rules
    # attached. Falls back to live HTML fetch when Neo4j is empty.)
    path("sops/<int:sop_id>/dom-tree/",
         SopDomTreeView.as_view(),
         name="sop-dom-tree"),
    path("sops/<int:sop_id>/dom-tree/<str:block_id>/",
         SopDomBlockView.as_view(),
         name="sop-dom-block"),

    # HTML Viewer
    path("viewer/",                                      ViewerJobListView.as_view(),   name="viewer-jobs"),
    path("viewer/<uuid:job_id>/",                        ViewerJobDetailView.as_view(), name="viewer-job"),
    path("viewer/<uuid:job_id>/doc/<int:doc_id>/",       ViewerDocDetailView.as_view(), name="viewer-doc"),
]
