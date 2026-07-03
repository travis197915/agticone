"""Find builder workflows impacted by a SopDocument or its ingested versions."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from uhc_sop_ingestion.revision import normalize_canonical_url

from ..models import AuditSop, IngestionJob, SopDocument


def _safe_node_rule_binding():
    try:
        from agent_tools.models import NodeRuleBinding
    except Exception:
        return None
    return NodeRuleBinding


def find_affected_workflows(
    document: SopDocument,
    *,
    sop_id: int | None = None,
    current_bindings_only: bool = False,
) -> dict[str, Any]:
    """Return workflows whose canvas shapes bind rules from this SOP document."""
    NodeRuleBinding = _safe_node_rule_binding()

    bindings_qs = None
    if NodeRuleBinding is not None:
        bindings_qs = (
            NodeRuleBinding.objects.filter(sop__document_id=document.id)
            .select_related(
                "sop",
                "shape",
                "shape__workbench",
                "shape__workbench__work_area",
                "shape__workbench__work_area__workflow",
            )
            .order_by(
                "shape__workbench__work_area__workflow__name",
                "shape__label",
                "ordering",
                "rule_key",
            )
        )
        if sop_id is not None:
            bindings_qs = bindings_qs.filter(sop_id=sop_id)
        if current_bindings_only:
            bindings_qs = bindings_qs.filter(sop__is_current=True)

    workflow_map: dict[str, dict[str, Any]] = {}
    shape_map: dict[tuple[str, str], dict[str, Any]] = defaultdict(dict)
    bound_versions: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)

    binding_count = 0
    if bindings_qs is not None:
        for binding in bindings_qs:
            shape = binding.shape
            wf = shape.workbench.work_area.workflow
            wf_id = str(wf.id)
            binding_count += 1

            if wf_id not in workflow_map:
                workflow_map[wf_id] = {
                    "workflow_id": wf_id,
                    "workflow_name": wf.name,
                    "is_active": wf.is_active,
                    "shapes": [],
                    "ingestion_jobs": [],
                    "bound_sop_versions": [],
                    "shape_count": 0,
                    "rule_binding_count": 0,
                }

            wf_entry = workflow_map[wf_id]
            wf_entry["rule_binding_count"] += 1

            sop = binding.sop
            if sop.id not in bound_versions[wf_id]:
                bound_versions[wf_id][sop.id] = {
                    "sop_id": sop.id,
                    "version_number": sop.version_number,
                    "is_current": sop.is_current,
                    "activation_status": sop.activation_status,
                    "revision_date": sop.revision_date,
                    "binding_count": 0,
                }
            bound_versions[wf_id][sop.id]["binding_count"] += 1

            shape_key = (wf_id, str(shape.id))
            if shape_key not in shape_map:
                shape_entry = {
                    "shape_id": str(shape.id),
                    "shape_label": shape.label or "",
                    "work_area_name": shape.workbench.work_area.name,
                    "workbench_name": shape.workbench.name,
                    "rules": [],
                }
                shape_map[shape_key] = shape_entry
                wf_entry["shapes"].append(shape_entry)
                wf_entry["shape_count"] += 1

            shape_map[shape_key]["rules"].append({
                "binding_id": str(binding.id),
                "rule_key": binding.rule_key,
                "sop_id": sop.id,
                "version_number": sop.version_number,
                "is_current": sop.is_current,
                "activation_status": sop.activation_status,
                "condition": binding.condition,
                "action": binding.action,
            })

    canonical = normalize_canonical_url(document.canonical_url)
    ingestion_by_workflow: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in (
        IngestionJob.objects.filter(workflow__isnull=False)
        .select_related("workflow")
        .prefetch_related("audit_sops")
        .order_by("-created_at")
    ):
        if normalize_canonical_url(job.seed_url) != canonical:
            continue
        wf_id = str(job.workflow_id)
        first_sop = job.audit_sops.order_by("id").first()
        ingestion_by_workflow[wf_id].append({
            "job_id": str(job.job_id),
            "status": job.status,
            "trigger_source": job.trigger_source,
            "created_at": job.created_at,
            "audit_sop_id": first_sop.id if first_sop else None,
        })
        if wf_id not in workflow_map:
            wf = job.workflow
            workflow_map[wf_id] = {
                "workflow_id": wf_id,
                "workflow_name": wf.name,
                "is_active": wf.is_active,
                "shapes": [],
                "ingestion_jobs": [],
                "bound_sop_versions": [],
                "shape_count": 0,
                "rule_binding_count": 0,
            }

    workflows = []
    for wf_id, entry in sorted(workflow_map.items(), key=lambda x: x[1]["workflow_name"].lower()):
        entry["bound_sop_versions"] = sorted(
            bound_versions.get(wf_id, {}).values(),
            key=lambda v: v["version_number"],
            reverse=True,
        )
        entry["ingestion_jobs"] = ingestion_by_workflow.get(wf_id, [])
        entry["has_rule_bindings"] = entry["rule_binding_count"] > 0
        current_sop_id = document.current_version_id
        entry["needs_rebind_after_version_change"] = bool(
            current_sop_id
            and entry["bound_sop_versions"]
            and any(v["sop_id"] != current_sop_id for v in entry["bound_sop_versions"])
        )
        workflows.append(entry)

    current = document.current_version
    return {
        "document_id": document.id,
        "canonical_url": document.canonical_url,
        "title": document.title,
        "current_sop_id": document.current_version_id,
        "current_revision_date": (
            current.revision_date if current else document.latest_revision_date
        ),
        "summary": {
            "workflow_count": len(workflows),
            "workflows_with_rule_bindings": sum(1 for w in workflows if w["rule_binding_count"]),
            "workflows_with_ingestion_only": sum(
                1 for w in workflows if w["ingestion_jobs"] and not w["rule_binding_count"]
            ),
            "shape_count": sum(w["shape_count"] for w in workflows),
            "rule_binding_count": binding_count,
        },
        "workflows": workflows,
    }
