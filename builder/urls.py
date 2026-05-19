"""
URL routes for the builder API.

Mounted at `/api/builder/` from `sop_backend/urls.py`.
"""
from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    DashboardWidgetViewSet,
    NavItemViewSet,
    ShapeCategoryViewSet,
    ShapeDefinitionViewSet,
    ShapeViewSet,
    WorkbenchViewSet,
    WorkflowViewSet,
)


app_name = "builder"

router = DefaultRouter()
# Catalog
router.register(r"catalog/categories", ShapeCategoryViewSet, basename="shape-category")
router.register(r"catalog/shapes",     ShapeDefinitionViewSet, basename="shape-definition")
# Server-driven chrome
router.register(r"ui/navigation",      NavItemViewSet, basename="nav-item")
router.register(r"ui/dashboard",       DashboardWidgetViewSet, basename="dashboard-widget")
# Domain
router.register(r"workflows",          WorkflowViewSet)
router.register(r"workbenches",        WorkbenchViewSet)
router.register(r"shapes",             ShapeViewSet)


urlpatterns = [path("", include(router.urls))]
