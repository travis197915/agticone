"""Seed the DB-backed SOP field mapping + claim ontology from the repo YAMLs.

Idempotent upsert: re-running updates existing rows and adds new ones. This is
the one-time bootstrap that moves ``yaml/sop_field_mapping.yaml`` and
``yaml/claim_ontology.yaml`` into Postgres (the runtime source of truth), and is
also how a fresh/prod environment gets populated.

Usage::

    python manage.py seed_field_mapping            # both, from BASE_DIR/yaml
    python manage.py seed_field_mapping --yaml-dir /path/to/yaml
    python manage.py seed_field_mapping --only mapping
    python manage.py seed_field_mapping --only ontology
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from django.conf import settings
from django.core.management.base import BaseCommand

from agent_tools.models import ClaimOntologyField, SopFieldMapping

# Lightweight keyword → category mapping so the UI groups fields sensibly out
# of the box. Purely cosmetic; editable later per row.
_CATEGORY_RULES = [
    ("Provider", ("provider", "npi", "tin", "tax id", "remit", "network", "entity", "payee")),
    ("Member", ("member", "subscriber", "patient", "insured", "eligibility", "suffix", "health id")),
    ("Coverage", ("coverage", "plan", "benefit", "covered", "cbd", "authorization", "cob")),
    ("Dates", ("date", "received")),
    ("Money", ("amount", "charge", "copay", "deductible", "paid", "rate")),
    ("Claim", ("claim", "eob", "procedure", "diagnosis", "units", "line item", "place of service", "status", "service")),
]


def _guess_category(field: str) -> str:
    f = field.lower()
    for category, keywords in _CATEGORY_RULES:
        if any(k in f for k in keywords):
            return category
    return ""


def _yaml_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("SOP_YAML_DIR")
    if env:
        return Path(env)
    return Path(getattr(settings, "BASE_DIR", ".")) / "yaml"


class Command(BaseCommand):
    help = "Seed SopFieldMapping + ClaimOntologyField rows from the repo YAMLs."

    def add_arguments(self, parser):
        parser.add_argument("--yaml-dir", default=None)
        parser.add_argument("--only", choices=["mapping", "ontology"], default=None)

    def handle(self, *args, **opts):
        d = _yaml_dir(opts.get("yaml_dir"))
        only = opts.get("only")

        if only in (None, "mapping"):
            self._seed_mapping(d / "sop_field_mapping.yaml")
        if only in (None, "ontology"):
            self._seed_ontology(d / "claim_ontology.yaml")

    def _seed_mapping(self, path: Path) -> None:
        try:
            doc = yaml.safe_load(open(path)) or {}
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"could not read {path}: {exc}"))
            return
        mappings = doc.get("mappings") or {}
        created = updated = 0
        for field, systems in mappings.items():
            field = str(field).strip()
            if not field:
                continue
            _, was_created = SopFieldMapping.objects.update_or_create(
                sop_field=field,
                defaults={
                    "systems": systems if isinstance(systems, dict) else {},
                    "category": _guess_category(field),
                    "is_active": True,
                },
            )
            created += int(was_created)
            updated += int(not was_created)
        self.stdout.write(self.style.SUCCESS(
            f"SopFieldMapping: {created} created, {updated} updated "
            f"({SopFieldMapping.objects.count()} total)"))

    def _seed_ontology(self, path: Path) -> None:
        try:
            doc = yaml.safe_load(open(path)) or {}
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"could not read {path}: {exc}"))
            return
        namespaces = doc.get("namespaces") or {}
        created = updated = 0
        for ns, fields in namespaces.items():
            if not isinstance(fields, dict):
                continue
            for field, spec in fields.items():
                aliases = []
                if isinstance(spec, dict):
                    aliases = spec.get("aliases") or []
                _, was_created = ClaimOntologyField.objects.update_or_create(
                    namespace=str(ns).strip(),
                    canonical_field=str(field).strip(),
                    defaults={"aliases": list(aliases), "is_active": True},
                )
                created += int(was_created)
                updated += int(not was_created)
        self.stdout.write(self.style.SUCCESS(
            f"ClaimOntologyField: {created} created, {updated} updated "
            f"({ClaimOntologyField.objects.count()} total)"))
