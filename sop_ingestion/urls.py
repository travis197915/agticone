from django.urls import path
from .views import (HealthView, JobListCreateView, JobDetailView, SyncRunView,
                    JobGraphView, JobSectionsView, JobContextualizeView,
                    ViewerJobListView, ViewerJobDetailView, ViewerDocDetailView)

app_name = "sop_ingestion"

urlpatterns = [
    # REST API
    path("",                     JobListCreateView.as_view(), name="job-list-create"),
    path("health/",              HealthView.as_view(),        name="health"),
    path("run-sync/",            SyncRunView.as_view(),       name="run-sync"),
    path("<uuid:job_id>/",       JobDetailView.as_view(),     name="job-detail"),
    path("<uuid:job_id>/graph/",    JobGraphView.as_view(),    name="job-graph"),
    path("<uuid:job_id>/sections/",      JobSectionsView.as_view(),      name="job-sections"),
    path("<uuid:job_id>/contextualize/", JobContextualizeView.as_view(), name="job-contextualize"),

    # HTML Viewer
    path("viewer/",                                      ViewerJobListView.as_view(),   name="viewer-jobs"),
    path("viewer/<uuid:job_id>/",                        ViewerJobDetailView.as_view(), name="viewer-job"),
    path("viewer/<uuid:job_id>/doc/<int:doc_id>/",       ViewerDocDetailView.as_view(), name="viewer-doc"),
]
