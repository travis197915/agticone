from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/",         admin.site.urls),
    path("api/ingest/",    include("sop_ingestion.urls", namespace="sop_ingestion")),
    path("api/builder/",   include("builder.urls", namespace="builder")),
    path("api/agent-tools/", include("agent_tools.urls", namespace="agent_tools")),
    path("api/mocks/",     include("agent_tools.mock.urls", namespace="agent_tools_mocks")),
    path("api/execute/",   include("execution_app.urls", namespace="execution_app")),
]
