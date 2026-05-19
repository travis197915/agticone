from django.apps import AppConfig


class BuilderConfig(AppConfig):
    """
    Django app that powers the drag-and-drop workflow builder.

    Owns:
      • The hierarchical workflow model
            Workflow → WorkArea → Workbench → Shape (+ ShapeConnection edges)
      • The server-driven UI catalog:  ShapeCategory, ShapeDefinition,
            NavItem, DashboardWidget — every drag-target and chrome element
            in the frontend is described here, never hardcoded.
      • REST endpoints under /api/builder/.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "builder"
    verbose_name = "Workflow Builder"
