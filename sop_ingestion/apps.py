from django.apps import AppConfig


class SopIngestionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name               = "sop_ingestion"
    verbose_name       = "SOP Ingestion Pipeline"
