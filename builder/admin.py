from django.contrib import admin

from .models import (
    DashboardWidget,
    NavItem,
    Shape,
    ShapeCategory,
    ShapeConnection,
    ShapeDefinition,
    WorkArea,
    Workbench,
    Workflow,
)


class WorkAreaInline(admin.TabularInline):
    model = WorkArea
    extra = 0


class WorkbenchInline(admin.TabularInline):
    model = Workbench
    extra = 0


class ShapeInline(admin.TabularInline):
    model = Shape
    extra = 0


@admin.register(Workflow)
class WorkflowAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "owner_email", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "owner_email")
    inlines = [WorkAreaInline]


@admin.register(WorkArea)
class WorkAreaAdmin(admin.ModelAdmin):
    list_display = ("name", "workflow", "order")
    list_filter = ("workflow",)
    search_fields = ("name", "workflow__name")
    inlines = [WorkbenchInline]


@admin.register(Workbench)
class WorkbenchAdmin(admin.ModelAdmin):
    list_display = ("name", "work_area", "node_key", "kind", "order")
    list_filter = ("kind",)
    search_fields = ("name", "node_key", "work_area__name")
    inlines = [ShapeInline]


@admin.register(Shape)
class ShapeAdmin(admin.ModelAdmin):
    list_display = ("label", "definition", "workbench", "order")
    list_filter = ("definition__category", "definition")
    search_fields = ("label", "workbench__name")


@admin.register(ShapeConnection)
class ShapeConnectionAdmin(admin.ModelAdmin):
    list_display = ("source_shape", "target_shape", "label", "condition_label")
    search_fields = ("label", "source_shape__label", "target_shape__label")


@admin.register(ShapeCategory)
class ShapeCategoryAdmin(admin.ModelAdmin):
    list_display = ("label", "slug", "order", "is_active")
    list_filter = ("is_active",)


@admin.register(ShapeDefinition)
class ShapeDefinitionAdmin(admin.ModelAdmin):
    list_display = ("label", "slug", "category", "kind", "order", "is_active")
    list_filter = ("category", "kind", "is_active")
    search_fields = ("label", "slug")


@admin.register(NavItem)
class NavItemAdmin(admin.ModelAdmin):
    list_display = ("label", "section", "href", "min_role", "order", "is_active")
    list_filter = ("section", "min_role", "is_active")


@admin.register(DashboardWidget)
class DashboardWidgetAdmin(admin.ModelAdmin):
    list_display = ("label", "kind", "order", "is_active")
    list_filter = ("kind", "is_active")
