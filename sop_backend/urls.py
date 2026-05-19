from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/",        admin.site.urls),
    path("api/ingest/",   include("sop_ingestion.urls", namespace="sop_ingestion")),
    path("api/builder/",  include("builder.urls", namespace="builder")),
]
